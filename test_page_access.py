"""
Per-group PAGE access control (app/groups.py `require_page`).

The Admin UI presents "which pages can this group's members reach" as access control, but until
2026-07-28 it was enforced only by hiding nav tabs in the browser — a restricted member could
still call the endpoints directly and get everything. These assert the backend gate:

  * an account in NO group is unrestricted (the overwhelmingly common case — this must stay a
    no-op, or every normal user breaks),
  * a group with NO page rows configured is unrestricted (so shipping this regressed nobody),
  * a group restricted to some pages 403s the ones it wasn't given, and allows the ones it was,
  * the PUBLIC customer build-status link is never gated — its whole point is a customer with no
    account and therefore no group, so a page gate would 403 every one of them.

Seeds real rows and cleans up after itself, same approach as test_skill_enough.py.
Run inside the container:
    docker exec eve-pi-planner-web-1 python3 test_page_access.py
"""

import secrets
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")
from fastapi import HTTPException  # noqa: E402

from app.sde import get_connection  # noqa: E402
from app.groups import (  # noqa: E402
    PAGE_REGISTRY, caller_allowed_pages, require_page, ensure_group_tables,
)

FAKE_CTX = 777401
FAKE_CID = 990401
FAKE_ALLIANCE = 99000401
GROUP_NAME = "ZZ Test Group (page-access)"

_failures = []


def check(cond, msg):
    ok = bool(cond)
    print(f"  {'PASS' if ok else 'FAIL'}: {msg}")
    if not ok:
        _failures.append(msg)
    return ok


def _group_id(con):
    row = con.execute("SELECT id FROM pp_groups WHERE alliance_id=?", (FAKE_ALLIANCE,)).fetchone()
    return row["id"] if row else None


def _cleanup():
    con = get_connection()
    gid = _group_id(con)
    if gid is not None:
        con.execute("DELETE FROM pp_group_pages WHERE group_id=?", (gid,))
        con.execute("DELETE FROM pp_groups WHERE id=?", (gid,))
    con.execute("DELETE FROM pp_characters WHERE character_id=?", (FAKE_CID,))
    con.execute("DELETE FROM pp_sessions WHERE context_id=?", (FAKE_CTX,))
    con.commit()
    con.close()


def _seed_character_without_group():
    con = get_connection()
    con.execute(
        "INSERT INTO pp_characters (character_id, character_name, context_id, alliance_id) "
        "VALUES (?,?,?,?) ON CONFLICT (character_id) DO UPDATE SET "
        "context_id=excluded.context_id, alliance_id=excluded.alliance_id",
        (FAKE_CID, "Page Gate Toon", FAKE_CTX, FAKE_ALLIANCE))
    con.commit()
    con.close()


def _seed_group(pages):
    """Create the group for FAKE_ALLIANCE and restrict it to `pages` ([] = no rows = unrestricted)."""
    con = get_connection()
    if _group_id(con) is None:
        con.execute("INSERT INTO pp_groups (name, alliance_id, created_at) VALUES (?,?,?)",
                    (GROUP_NAME, FAKE_ALLIANCE, "2026-07-28T00:00:00"))
        con.commit()
    gid = _group_id(con)
    con.execute("DELETE FROM pp_group_pages WHERE group_id=?", (gid,))
    for key in pages:
        con.execute("INSERT INTO pp_group_pages (group_id, page_key) VALUES (?,?)", (gid, key))
    con.commit()
    con.close()
    return gid


def _fabricate_session():
    """A real pp_sessions row, so the dependency resolves the context the way a request does."""
    token = secrets.token_hex(16)
    con = get_connection()
    con.execute("DELETE FROM pp_sessions WHERE context_id=?", (FAKE_CTX,))
    con.execute("INSERT INTO pp_sessions (token, character_id, context_id, created_at) VALUES (?,?,?,?)",
                (token, FAKE_CID, FAKE_CTX, datetime.now(timezone.utc).isoformat()))
    con.commit()
    con.close()
    return token


def _denies(page_key, token):
    """Invoke the REAL dependency callable with a real session cookie — not a reimplementation
    of its logic, which would pass even if require_page were broken."""
    dep = require_page(page_key)
    try:
        dep(pp_session=token)
        return False
    except HTTPException as e:
        return e.status_code == 403


def main():
    ensure_group_tables()
    _cleanup()
    try:
        # ── industry is restrictable at all ────────────────────────────────────────────────
        keys = {p["key"] for p in PAGE_REGISTRY}
        check("industry" in keys,
              "the Industry tab appears in PAGE_REGISTRY so a group can actually restrict it")
        check("reactions" in keys, "Reactions is restrictable")

        # ── no group at all → unrestricted ─────────────────────────────────────────────────
        _seed_character_without_group()
        check(caller_allowed_pages(FAKE_CTX) is None,
              "an account whose alliance has no group is unrestricted (None)")

        # ── group with no page rows → still unrestricted ───────────────────────────────────
        _seed_group([])
        check(caller_allowed_pages(FAKE_CTX) is None,
              "a group with zero page rows is unrestricted, so shipping this regressed nobody")

        # ── group restricted to a subset ───────────────────────────────────────────────────
        _seed_group(["dashboard", "reactions"])
        token = _fabricate_session()
        allowed = caller_allowed_pages(FAKE_CTX)
        check(allowed is not None and set(allowed) == {"dashboard", "reactions"},
              f"a restricted group reports exactly its pages (got {allowed})")
        check(_denies("industry", token),
              "a page the group was NOT given is refused with 403")
        check(not _denies("reactions", token),
              "a page the group WAS given is allowed through")
        check(not _denies("dashboard", token),
              "and so is the other one")

        check(not _denies("industry", None),
              "an anonymous caller passes the page gate — each endpoint's own require_context "
              "still rejects it, and a customer link must not be 403'd by a group rule")

        # ── the public customer link is not behind the gate ────────────────────────────────
        from app.industry import _router as ind_router
        from app.industry import shares as ind_shares
        import inspect
        gated = [d for d in (ind_router.router.dependencies or [])]
        check(len(gated) >= 1,
              "the account-scoped industry router carries the page dependency")
        check(not (ind_router.public_router.dependencies or []),
              "the public router carries NO page dependency")
        src = inspect.getsource(ind_shares)
        check("@public_router.get(\"/api/industry/build-status/{share_id}\")" in src,
              "the customer build-status link is registered on the ungated public router — a "
              "customer has no account and therefore no group, so gating it would 403 all of them")
    finally:
        _cleanup()

    print()
    if _failures:
        print(f"  {len(_failures)} FAILED")
        return 1
    print("  ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
