#!/usr/bin/env python3
"""The hand-set middle state: not started → running → done, and the rule that keeps it honest.

Progress used to be two-valued by hand (marked done, or nothing) while "running" was inferred ONLY
from the ESI job caches — so a job installed on a character that never granted the jobs scope read
as *not started* right up until it completed. A mark can now say `running` too. What has to stay
true, and is what this pins:

  * A hand mark may only ever move a type FORWARD. `resolve_running` takes whatever `resolve_done`
    settled off the top first, so a manual "running" can never walk back a measured "done".
  * Rows written before the state column existed were done-marks and must still read as done —
    the one thing a migration here could visibly break for an existing user.
  * Neither state writes to the completion ledgers. A tick is a statement about this queue, not
    evidence of an ISK-bearing job.
  * Partial done-marks (the second-click path) still work, and the customer-share cache is
    invalidated by a `running` mark just as it is by a `done` one.

In-process, on a temporary SQLite DB of its own — no live ESI, no SDE. Run inside the container:

    docker compose cp test_progress_running.py web:/srv/app/ && \\
      docker compose exec web python3 test_progress_running.py
"""
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, ".")

failures = []


def check(msg, cond):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


# ── A throwaway SQLite DB standing in for the real one ────────────────────────────────────────
_TMP = tempfile.mkdtemp(prefix="progrun-")
_DB = os.path.join(_TMP, "t.db")


class _Con(sqlite3.Connection):
    def close(self):                 # the module closes what it borrows; keep the file alive
        pass

    def really_close(self):
        sqlite3.Connection.close(self)


_SHARED = sqlite3.connect(_DB, factory=_Con, check_same_thread=False)
_SHARED.row_factory = sqlite3.Row

import app.db as DB                                             # noqa: E402
DB.get_connection = lambda: _SHARED

import app.industry.progress as PR                              # noqa: E402
import app.industry.assets as ASSETS                            # noqa: E402

CTX = 4242
WIDGET, PART = 100, 110

PLAN = {"requirements": [
    {"type_id": WIDGET, "name": "Widget", "activity": "manufacturing", "runs": 4, "output_qty": 1},
    {"type_id": PART, "name": "Part", "activity": "reaction", "runs": 10, "output_qty": 2},
], "schedule": {"waves": [{"tasks": [
    {"type_id": PART, "duration_hours": 1.0}, {"type_id": WIDGET, "duration_hours": 3.0}]}]}}
ORDERS = [{"id": 1, "product_type_id": WIDGET, "name": "Widget", "quantity": 4, "label": "cust"}]


def _progress(completed=None, running=None, owned=None):
    """queue_progress with only the DB edges (marks) real; ESI/ledger/hangar edges are stubbed."""
    PR._queue_snapshot = lambda ctx, res=None: (ORDERS, PLAN)
    PR._epoch = lambda ctx: 0.0
    PR._done_by_type = lambda ctx, since: dict(completed or {})
    PR._running_by_type = lambda ctx, since: dict(running or {})
    ASSETS.owned_quantities = lambda ctx: dict(owned or {})
    p = PR.queue_progress(CTX)
    return {t["type_id"]: t for t in p["types"]}, p


def _clear():
    _SHARED.execute("DELETE FROM pp_industry_manual_done WHERE context_id=?", (CTX,))
    _SHARED.commit()


# ── 1. the migration ──────────────────────────────────────────────────────────────────────────
def test_pre_migration_marks_still_mean_done():
    """The one thing that would visibly break for an existing user. Build the table as it was
    BEFORE the state column, put a done-mark in it, then let the real migration run over it."""
    print("test_pre_migration_marks_still_mean_done")
    _SHARED.execute("""
        CREATE TABLE pp_industry_manual_done (
            context_id INTEGER NOT NULL, type_id INTEGER NOT NULL,
            runs INTEGER NOT NULL DEFAULT -1, marked_at REAL NOT NULL,
            PRIMARY KEY (context_id, type_id))
    """)
    _SHARED.execute("INSERT INTO pp_industry_manual_done VALUES (?,?,?,?)",
                    (CTX, PART, PR._ALL, time.time()))
    _SHARED.execute("INSERT INTO pp_industry_manual_done VALUES (?,?,?,?)",
                    (CTX, WIDGET, 2, time.time()))
    _SHARED.commit()
    cols = {r[1] for r in _SHARED.execute("PRAGMA table_info(pp_industry_manual_done)")}
    check("the old table genuinely has no state column", "state" not in cols)

    # ensure_once() caches "already ran" per process; go through to the real DDL.
    PR.ensure_manual_done_table.__wrapped__()
    cols = {r[1] for r in _SHARED.execute("PRAGMA table_info(pp_industry_manual_done)")}
    check("the migration added state additively (nothing dropped)",
          {"context_id", "type_id", "runs", "marked_at", "state"} <= cols)

    marked = PR._manual_by_type(CTX, 0.0)
    check("a pre-migration whole-step mark still reads done",
          marked[PART] == (PR._ALL, "done"))
    check("a pre-migration partial mark keeps its run count and reads done",
          marked[WIDGET] == (2, "done"))

    by, _ = _progress()
    check("and it still counts as done through the real payload",
          by[PART]["done_runs"] == 10 and by[WIDGET]["done_runs"] == 2)
    check("with the state reported so the UI knows where it stands",
          by[PART]["manual_state"] == "done")
    _clear()


# ── 2. the three-state cycle ──────────────────────────────────────────────────────────────────
def test_three_state_cycle():
    """not started → running → done → not started, driven exactly as the frontend drives it."""
    print("test_three_state_cycle")
    by, _ = _progress()
    check("starts not started", by[PART]["done_runs"] == 0 and by[PART]["running_runs"] == 0
          and by[PART]["waiting_runs"] == 10 and by[PART]["manual_state"] == "")

    PR.set_manual_done(CTX, PART, None, "running")
    by, p = _progress()
    check("running: the whole step is in flight", by[PART]["running_runs"] == 10)
    check("running: and none of it is done", by[PART]["done_runs"] == 0)
    check("running: nothing is left waiting", by[PART]["waiting_runs"] == 0)
    check("running: reported as a hand mark", by[PART]["manual_state"] == "running")
    check("running: does NOT count toward the done headline", p["totals"]["done"] == 0)
    check("running: manual_runs stays 0 — it is not a done-signal", by[PART]["manual_runs"] == 0)

    PR.set_manual_done(CTX, PART, None, "done")
    by, p = _progress()
    check("done: the whole step is done", by[PART]["done_runs"] == 10)
    check("done: nothing left running", by[PART]["running_runs"] == 0)
    check("done: the running mark was replaced, not added to",
          by[PART]["manual_state"] == "done"
          and _SHARED.execute("SELECT COUNT(*) c FROM pp_industry_manual_done WHERE context_id=? "
                              "AND type_id=?", (CTX, PART)).fetchone()["c"] == 1)

    PR.set_manual_done(CTX, PART, 0)
    by, _ = _progress()
    check("cleared: back to not started, so a misclick is recoverable",
          by[PART]["done_runs"] == 0 and by[PART]["running_runs"] == 0
          and by[PART]["waiting_runs"] == 10 and by[PART]["manual_state"] == "")
    _clear()


# ── 3. the never-override rule ────────────────────────────────────────────────────────────────
def test_manual_running_never_walks_a_measured_done_backwards():
    print("test_manual_running_never_walks_a_measured_done_backwards")
    # Pure arithmetic first — the precedence is stated in one function, so pin it there too.
    check("resolve_running: done is taken off the top before anything else",
          PR.resolve_running(10, 10, 0, 10) == 0)
    check("resolve_running: a mark can only fill what is left",
          PR.resolve_running(10, 6, 0, 10) == 4)
    check("resolve_running: the higher of the two running signals wins",
          PR.resolve_running(10, 0, 3, 10) == 10 and PR.resolve_running(10, 0, 7, 2) == 7)
    check("resolve_done ignores a running mark entirely (it is not a done-signal)",
          PR.resolve_done(10, 0, 0, 0) == 0)

    PR.set_manual_done(CTX, PART, None, "running")
    by, _ = _progress(completed={PART: 10})
    check("ESI says the batch is delivered — a hand 'running' cannot un-finish it",
          by[PART]["done_runs"] == 10 and by[PART]["running_runs"] == 0)
    check("and the type reads 100%", by[PART]["pct"] == 100.0)

    by, _ = _progress(completed={PART: 6})
    check("partly delivered: the mark only fills the remainder",
          by[PART]["done_runs"] == 6 and by[PART]["running_runs"] == 4)

    # The hangar is the other measured done-signal: 20 Parts at output_qty 2 = 10 runs' worth.
    by, _ = _progress(owned={PART: 20})
    check("owned stock outranks a hand 'running' the same way",
          by[PART]["done_runs"] == 10 and by[PART]["running_runs"] == 0)

    # ...and the reverse direction: measured running higher than the mark still wins.
    _clear()
    PR.set_manual_done(CTX, PART, 3, "running")     # a 3-run running mark
    by, _ = _progress(running={PART: 8})
    check("a bigger measured running count is not hidden by a smaller mark",
          by[PART]["running_runs"] == 8)
    _clear()

    # A DONE mark still cannot hide a bigger measured done count either — unchanged behaviour.
    PR.set_manual_done(CTX, PART, 2, "done")
    by, _ = _progress(completed={PART: 7})
    check("and a done mark still never lowers a measured done count", by[PART]["done_runs"] == 7)
    check("observed_runs reports the count with the mark removed",
          by[PART]["observed_runs"] == 7)
    _clear()


# ── 4. observed_* round-trip (what makes the optimistic repaint honest) ────────────────────────
def test_observed_counts_exclude_the_mark():
    print("test_observed_counts_exclude_the_mark")
    PR.set_manual_done(CTX, PART, None, "running")
    by, _ = _progress(running={PART: 2})
    check("resolved running includes the mark", by[PART]["running_runs"] == 10)
    check("observed_running_runs is what ESI alone saw",
          by[PART]["observed_running_runs"] == 2)
    check("observed_runs is untouched by a running mark", by[PART]["observed_runs"] == 0)
    _clear()


# ── 5. the order roll-up ──────────────────────────────────────────────────────────────────────
def test_a_running_mark_moves_the_order_chip():
    print("test_a_running_mark_moves_the_order_chip")
    _, p = _progress()
    check("no mark: the order is waiting", p["orders"][0]["status"] == "waiting")
    PR.set_manual_done(CTX, WIDGET, None, "running")
    _, p = _progress()
    row = p["orders"][0]
    check("a running mark makes the order read 'building'", row["status"] == "building")
    check("in units, not runs", row["running_units"] == 4 and row["quantity"] == 4)
    check("but nothing is claimed as delivered", row["done_units"] == 0 and row["pct"] == 0.0)
    PR.set_manual_done(CTX, WIDGET, None, "done")
    _, p = _progress()
    check("marking it done completes the order", p["orders"][0]["status"] == "complete")
    _clear()


# ── 6. partial done marks still work ──────────────────────────────────────────────────────────
def test_partial_done_marks_still_work():
    """The second-click path (indEditDoneRuns → indApplyDoneRuns → runs=N, state defaults done)."""
    print("test_partial_done_marks_still_work")
    PR.set_manual_done(CTX, PART, 5)
    by, _ = _progress()
    check("five of ten runs marked done", by[PART]["done_runs"] == 5)
    check("reported as a hand mark of that size",
          by[PART]["manual_runs"] == 5 and by[PART]["manual_state"] == "done")
    check("the rest is still waiting", by[PART]["waiting_runs"] == 5)
    check("a partial mark caps at the requirement", (PR.set_manual_done(CTX, PART, 99),
                                                     _progress()[0][PART]["done_runs"] == 10)[1])
    # A partial mark and a measured running job coexist: 5 done, 3 in flight, 2 waiting.
    PR.set_manual_done(CTX, PART, 5)
    by, _ = _progress(running={PART: 3})
    check("a partial done mark leaves room for measured running work",
          (by[PART]["done_runs"], by[PART]["running_runs"], by[PART]["waiting_runs"]) == (5, 3, 2))
    _clear()


# ── 7. the ledgers, and the share cache ───────────────────────────────────────────────────────
def test_marks_never_touch_the_earnings_ledgers():
    """Both states are statements about THIS queue's progress, not evidence of an ISK-bearing job.
    Writing either into the completion ledgers would corrupt lifetime turnover permanently."""
    print("test_marks_never_touch_the_earnings_ledgers")
    for t in ("pp_industry_completions", "pp_reaction_completions"):
        _SHARED.execute(f"CREATE TABLE IF NOT EXISTS {t} (context_id INTEGER, product_type_id "
                        "INTEGER, runs INTEGER, completed_at REAL)")
    _SHARED.commit()

    def _rows():
        return sum(_SHARED.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                   for t in ("pp_industry_completions", "pp_reaction_completions"))

    before = _rows()
    PR.set_manual_done(CTX, PART, None, "running")
    check("a running mark writes no completion rows", _rows() == before)
    PR.set_manual_done(CTX, PART, None, "done")
    check("nor does a done mark", _rows() == before)
    PR.set_manual_done(CTX, PART, 3, "done")
    check("nor does a partial one", _rows() == before)
    # And the source says so, not just this run of it.
    import inspect
    src = inspect.getsource(PR.set_manual_done) + inspect.getsource(PR.industry_mark_done)
    check("no ledger table is named anywhere on the write path",
          "pp_industry_completions" not in src and "pp_reaction_completions" not in src)
    _clear()


def test_a_running_mark_invalidates_customer_shares():
    """A customer's progress bar moves on a running mark too — a stale cached share is exactly the
    doubt the status link exists to remove."""
    print("test_a_running_mark_invalidates_customer_shares")
    import app.industry.shares as SH
    calls = []
    orig = SH.invalidate_context_shares
    SH.invalidate_context_shares = lambda ctx: calls.append(ctx)
    try:
        PR._queue_snapshot = lambda ctx, res=None: (None, None)   # keep the refresh cheap
        for state in ("running", "done"):
            PR.industry_mark_done(PR.MarkDone(type_id=PART, runs=None, state=state), ctx=CTX)
        PR.industry_mark_done(PR.MarkDone(type_id=PART, runs=0), ctx=CTX)
    finally:
        SH.invalidate_context_shares = orig
    check("every mark invalidates the account's shares", calls == [CTX, CTX, CTX])

    # The endpoint must actually carry the state through to storage, not drop it on the floor.
    PR.industry_mark_done(PR.MarkDone(type_id=PART, runs=None, state="running"), ctx=CTX)
    check("the endpoint stores the state it was given",
          PR._manual_by_type(CTX, 0.0).get(PART) == (PR._ALL, "running"))
    check("an unknown state falls back to done rather than storing nonsense",
          (PR.set_manual_done(CTX, PART, None, "banana"),
           PR._manual_by_type(CTX, 0.0).get(PART) == (PR._ALL, "done"))[1])
    _clear()


def main():
    test_pre_migration_marks_still_mean_done()
    test_three_state_cycle()
    test_manual_running_never_walks_a_measured_done_backwards()
    test_observed_counts_exclude_the_mark()
    test_a_running_mark_moves_the_order_chip()
    test_partial_done_marks_still_work()
    test_marks_never_touch_the_earnings_ledgers()
    test_a_running_mark_invalidates_customer_shares()
    print()
    if failures:
        print(f"{len(failures)} FAILED:")
        for f in failures:
            print("  - " + f)
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
