#!/usr/bin/env python3
"""Who pressed the dangerous button, and who went looking for one.

On 2026-08-15 the shared planet database was found empty — 5,302 rows, the reference data the whole
PI planner runs on. Answering "when" needed eight nightly dumps diffed to bracket it to a DAY, and
"who" was never answered at all, because nothing recorded it.

This pins the log that fixes that, and — more importantly — pins the SHAPE that keeps it readable.
An audit log everyone ignores is one that records everything, so:

  * a destructive action writes exactly one row, with the actor resolved AT WRITE TIME (a join would
    lose precisely the rows about deleted accounts);
  * a refused attempt by a LOGGED-IN user is recorded; anonymous traffic is not, because the
    internet scans every host constantly and that noise is how the signal gets buried;
  * repeats inside the dedupe window collapse, so a scanner cannot flood the table;
  * recording NEVER breaks the action it describes — a broken audit table must not turn a working
    delete into a 500.

In-process; run inside the container against a NON-PROD database.

    docker compose cp tests/test_audit.py web:/srv/app/tests/ && \
      docker compose exec web python3 tests/test_audit.py
"""
import sys
import time

sys.path.insert(0, ".")

_fails = []
CTX = 717171


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


def _clear():
    from app.db import get_connection
    con = get_connection()
    try:
        con.execute("DELETE FROM pp_audit_log WHERE action LIKE 'test.%' OR context_id=?", (CTX,))
        con.commit()
    finally:
        con.close()


def main() -> int:
    from app import audit
    from app.audit import (record, recent, record_denied, ensure_audit_table,
                           GLOBAL_SCOPE, ACCOUNT_SCOPE, SECURITY_SCOPE)
    ensure_audit_table()
    _clear()
    audit._recent_denials.clear()

    print("\na destructive action leaves one row that says what and how much:")
    record("test.wipe", scope=GLOBAL_SCOPE, context_id=CTX, target="pp_thing", affected=5302,
           detail="cleared everything")
    rows = [r for r in recent(50) if r["action"] == "test.wipe"]
    check(len(rows) == 1, f"exactly one row (got {len(rows)})")
    if rows:
        r = rows[0]
        check(r["scope"] == GLOBAL_SCOPE, "the scope says it touched everyone's data")
        check(r["affected"] == 5302, f"the row count is kept (got {r['affected']})")
        check(r["at"] > time.time() - 60, "and it is timestamped now")

    print("\nnewest first, because that is the only order anyone reads it in:")
    record("test.second", scope=ACCOUNT_SCOPE, context_id=CTX, target="x")
    allr = [r for r in recent(50) if r["action"].startswith("test.")]
    check(allr and allr[0]["action"] == "test.second",
          f"the most recent entry leads (got {allr[0]['action'] if allr else None})")

    print("\nthe actor is stored, not joined — the rows about deleted accounts must survive:")
    cols = set(allr[0]) if allr else set()
    check("character_name" in cols,
          "the row carries a name column of its own")
    check("context_id" in cols, "...alongside the id, so an unnamed context is still identifiable")

    print("\na refused attempt by a LOGGED-IN user is recorded:")
    _clear(); audit._recent_denials.clear()
    record_denied("test.denied", context_id=CTX, target="admin endpoint", detail="nope")
    rows = [r for r in recent(50) if r["action"] == "test.denied"]
    check(len(rows) == 1, f"the denial is on the record (got {len(rows)})")
    check(rows and rows[0]["scope"] == SECURITY_SCOPE, "...under the security scope")

    print("\n...but a scanner cannot flood it:")
    for _ in range(50):
        record_denied("test.denied", context_id=CTX, target="admin endpoint", detail="nope")
    rows = [r for r in recent(200) if r["action"] == "test.denied"]
    check(len(rows) == 1, f"51 identical attempts are still one row (got {len(rows)})")
    record_denied("test.denied", context_id=CTX, target="a DIFFERENT path", detail="nope")
    rows = [r for r in recent(200) if r["action"] == "test.denied"]
    check(len(rows) == 2, f"a different target is a different event (got {len(rows)})")

    print("\nrecording can never break the thing it is recording:")
    # The whole point of the swallow: an audit table having a bad day must not turn a working
    # delete into a 500. Proven by making the write fail rather than by trusting the try/except.
    import app.audit as _a
    orig = _a.get_connection
    _a.get_connection = lambda: (_ for _ in ()).throw(RuntimeError("db is down"))
    try:
        record("test.explodes", scope=GLOBAL_SCOPE, context_id=CTX, target="x", affected=1)
        check(True, "a failed write raises nothing")
    except Exception as e:
        check(False, f"a failed write raised {type(e).__name__}")
    finally:
        _a.get_connection = orig

    _clear()
    guard_the_wiring()
    print("\n" + ("FAILED: " + "; ".join(_fails) if _fails else "all checks passed"))
    return 1 if _fails else 0


def guard_the_wiring() -> None:
    """The call sites, which the behavioural tests above cannot see.

    Everything above drives `record` directly. Deleting the call from `clear_planets` reintroduces
    the exact gap that made 2026-08-15 expensive and leaves every assertion above green — verified
    by doing it. So the endpoints that must report are asserted individually.
    """
    import ast
    import os
    print("\nthe endpoints that must report, do (source scan):")
    want = [
        ("app/planetary.py", "clear_planets", "the planet wipe — the one that started this"),
        ("app/esi.py", "delete_account", "account deletion"),
        ("app/admin.py", "run_cleanup", "the cross-account DB cleanup"),
        ("app/esi.py", "require_admin", "a non-admin refused an admin endpoint"),
    ]
    for path, fname, label in want:
        src = open(path, encoding="utf-8").read()
        tree = ast.parse(src)
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fname),
                  None)
        if fn is None:
            check(False, f"{fname} still exists in {path}")
            continue
        body = "\n".join(src.splitlines()[fn.lineno - 1:(fn.end_lineno or fn.lineno)])
        check("record(" in body or "record_denied(" in body, f"{label} is recorded ({fname})")

    # The 404 probe recorder must ignore anonymous traffic, or the table fills with scanner noise
    # and stops being read at all.
    main_src = open("app/main.py", encoding="utf-8").read()
    probe = main_src[main_src.index("def _note_probe"):]
    probe = probe[:probe.index("\napp.include_router")]
    check("session_context_id" in probe and "if ctx is None" in probe,
          "an anonymous 404 is ignored — only a known account probing is worth a row")
    check("_PROBE_IGNORE" in probe, "...and asset-shaped 404s are filtered out too")


if __name__ == "__main__":
    sys.exit(main())
