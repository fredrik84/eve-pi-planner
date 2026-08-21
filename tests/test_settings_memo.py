#!/usr/bin/env python3
"""The Industry settings row is read once per request, and a write is never read back stale.

Measured 2026-08-14 while answering §18's precomputation half: `account_setup` asked for
`get_settings` **six times** per call, each opening its own pooled connection, for a row that cannot
change while the request is in flight. Memoising it took the call from 9.4ms to 5.5ms on a local
dataset — small in absolute terms, but it is the same read repeated, which is exactly what §18 was
asking after.

**The risk memoising creates is the thing pinned here.** A cached settings row is only safe if every
writer drops it: a write followed by a read in the SAME request would otherwise return the value
from before the write, silently. That is worse than the read it saved, so:

  * a write is visible to the very next read in the same request, for every writer;
  * a fresh request never inherits the previous one's answer;
  * with no memo scope open at all (a script, a background job) the reader still works.

In-process; run inside the container against a NON-PROD database.

    docker compose cp tests/test_settings_memo.py web:/srv/app/tests/ && \
      docker compose exec web python3 tests/test_settings_memo.py
"""
import sys

sys.path.insert(0, ".")

_fails = []
CTX = 616161


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


def main() -> int:
    from app.cache import begin_request_memo
    from app.industry.settings import (get_settings, set_per_order_plans,
                                       set_max_reaction_job_days, get_max_reaction_job_days)

    print("\nwith no memo scope open the reader still answers (scripts, cron, tests):")
    d = get_settings(CTX)
    check(isinstance(d, dict), f"a plain dict comes back (got {type(d).__name__})")

    print("\na write is visible to the next read in the SAME request — every writer:")
    begin_request_memo()
    before = bool(get_settings(CTX).get("per_order_plans"))
    set_per_order_plans(CTX, not before)
    check(bool(get_settings(CTX).get("per_order_plans")) == (not before),
          "set_per_order_plans is not read back stale")
    set_per_order_plans(CTX, before)
    check(bool(get_settings(CTX).get("per_order_plans")) == before,
          "...and setting it back is visible too")

    begin_request_memo()
    prior = get_max_reaction_job_days(CTX)
    set_max_reaction_job_days(CTX, 9.0)
    check(get_settings(CTX).get("max_reaction_job_days") == 9.0,
          f"set_max_reaction_job_days is not read back stale (got {get_settings(CTX).get('max_reaction_job_days')})")
    set_max_reaction_job_days(CTX, prior)
    check(get_max_reaction_job_days(CTX) == prior, "...and is restored")

    print("\na FRESH request does not inherit the previous one's answer:")
    begin_request_memo()
    set_per_order_plans(CTX, not before)
    begin_request_memo()                      # new request
    check(bool(get_settings(CTX).get("per_order_plans")) == (not before),
          "the new request reads the row as it now stands")
    set_per_order_plans(CTX, before)

    print("\nthe memo is per CONTEXT, so one account cannot read another's settings:")
    begin_request_memo()
    set_per_order_plans(CTX, True)
    a = bool(get_settings(CTX).get("per_order_plans"))
    b = bool(get_settings(CTX + 1).get("per_order_plans"))
    check(a is True and b is False,
          f"two contexts get their own answers in one request (got {a}, {b})")
    set_per_order_plans(CTX, before)

    from app.sde import get_connection
    con = get_connection()
    try:
        con.execute("DELETE FROM pp_industry_settings WHERE context_id IN (?,?)", (CTX, CTX + 1))
        con.commit()
    finally:
        con.close()

    guard_every_writer()
    print("\n" + ("FAILED: " + "; ".join(_fails) if _fails else "all checks passed"))
    return 1 if _fails else 0


def guard_every_writer() -> None:
    """EVERY function that writes the settings row must drop the memo — checked structurally.

    Exercising writers one at a time cannot hold: there are ten, and the eleventh will be added by
    someone who has never read this file. Reintroducing the defect proved the point — removing the
    invalidation from a writer this test did not happen to call left it green. So this walks the
    source instead: any function containing an INSERT or UPDATE against `pp_industry_settings` must
    also call `_forget_settings_memo`. A new writer that forgets fails here, by construction.
    """
    import ast
    import os
    print("\nevery writer of the settings row drops the memo (source scan):")
    path = os.path.join("app", "industry", "settings.py")
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    lines = src.splitlines()
    offenders = []
    checked = 0
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        body = "\n".join(lines[fn.lineno - 1:(fn.end_lineno or fn.lineno)])
        writes = ("INSERT INTO pp_industry_settings" in body
                  or "UPDATE pp_industry_settings" in body)
        # `ensure_industry_settings_table` creates the table and writes no account row; it has no
        # context to forget and is the one legitimate exemption.
        if not writes or fn.name == "ensure_industry_settings_table":
            continue
        checked += 1
        if "_forget_settings_memo(" not in body:
            offenders.append(fn.name)
    check(checked >= 8, f"the scan actually found the writers ({checked} of them)")
    check(not offenders, f"no writer forgets to drop the memo (offenders: {offenders})")


if __name__ == "__main__":
    sys.exit(main())
