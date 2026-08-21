"""
Correctness tests for account deletion (DELETE /api/me).

This is the endpoint whose entire promise is "delete all my data", and it used to clear three
per-character tables and four context tables while orphaning rows in roughly twenty others. The
invariants worth guarding are therefore: nothing keyed to the account survives, nothing belonging
to ANOTHER account is touched, and the handful of deliberate exceptions (anonymised bug reports,
group-level rows) behave as documented rather than by accident.

Rows are seeded by introspecting each table and filling its NOT NULL columns with dummy values,
so a new column on any of these tables doesn't silently turn a seed into a skipped test. That
introspection uses PRAGMA table_info, so this suite must run against the container's SQLite —
which is where the whole suite runs anyway:

    docker cp tests/test_delete_account.py <container>:/srv/app/tests/ && \
      docker exec <container> python3 tests/test_delete_account.py [--url http://localhost:8000]
"""
import json
import secrets
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

sys.path.insert(0, ".")
from app.sde import get_connection  # noqa: E402
from app.esi import (  # noqa: E402
    ensure_char_tables, delete_account, _table_exists,
    _CHAR_OWNED_TABLES, _CONTEXT_OWNED_TABLES,
)

MINE_CTX = 779001
MINE_CID = 992001
MINE_CID2 = 992002
OTHER_CTX = 779002
OTHER_CID = 992003


def check(cond: bool, msg: str) -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    return bool(cond)


def _cols(con, table):
    return list(con.execute(f"PRAGMA table_info({table})"))


def _seed_row(con, table, **overrides):
    """Insert one row, filling every NOT NULL column that has no default with a dummy value."""
    vals = dict(overrides)
    # Index access, not tuple unpacking: the app's connection sets a Row factory whose rows
    # iterate as column NAMES, so `for a, b, ... in rows` silently yields the header instead.
    for r in _cols(con, table):
        name, ctype, notnull, dflt, pk = r[1], r[2], r[3], r[4], r[5]
        if name in vals:
            continue
        if pk and "INT" in (ctype or "").upper() and not notnull:
            continue                      # autoincrement rowid
        if not notnull or dflt is not None:
            continue
        t = (ctype or "TEXT").upper()
        vals[name] = 0 if ("INT" in t or "REAL" in t or "NUM" in t or "FLOAT" in t) else "x"
    cols = ",".join(vals)
    ph = ",".join("?" * len(vals))
    con.execute(f"INSERT INTO {table} ({cols}) VALUES ({ph})", list(vals.values()))


def _count(con, table, where, args):
    return con.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]


def _cleanup():
    con = get_connection()
    for cid in (MINE_CID, MINE_CID2, OTHER_CID):
        for t in _CHAR_OWNED_TABLES:
            if _table_exists(con, t):
                con.execute(f"DELETE FROM {t} WHERE character_id=?", (cid,))
        con.execute("DELETE FROM pp_characters WHERE character_id=?", (cid,))
    for ctx in (MINE_CTX, OTHER_CTX):
        for t in _CONTEXT_OWNED_TABLES + ["pp_baskets", "pp_bugs", "pp_sessions"]:
            if _table_exists(con, t):
                con.execute(f"DELETE FROM {t} WHERE context_id=?", (ctx,))
        if _table_exists(con, "pp_markets"):
            con.execute("DELETE FROM pp_markets WHERE owner_id=?", (ctx,))
        con.execute("DELETE FROM pp_user_contexts WHERE id=?", (ctx,))
    con.execute("DELETE FROM pp_bugs WHERE character_name=?", ("(deleted account)",))
    con.commit()
    con.close()


def _seed_account(ctx: int, char_ids: list, name_prefix: str):
    """A fully-populated account: characters, per-character rows, context rows, baskets, markets."""
    con = get_connection()
    con.execute("INSERT INTO pp_user_contexts (id, created_at) VALUES (?,?) "
                "ON CONFLICT (id) DO NOTHING", (ctx, datetime.now(timezone.utc).isoformat()))
    for i, cid in enumerate(char_ids):
        con.execute(
            "INSERT INTO pp_characters (character_id, character_name, context_id) VALUES (?,?,?) "
            "ON CONFLICT (character_id) DO UPDATE SET context_id=excluded.context_id",
            (cid, f"{name_prefix} {i}", ctx))
        for t in _CHAR_OWNED_TABLES:
            if not _table_exists(con, t):
                continue
            extra = {"character_id": cid}
            if t == "pp_char_planets":
                extra["planet_id"] = 5000 + i
            elif t == "pp_colony_yield":
                extra.update(planet_id=5000 + i, install_ts=1.0)
            elif t == "pp_colony_flags":
                extra.update(context_id=ctx, planet_id=5000 + i)
            elif t == "pp_plan_config":
                extra["product_type_id"] = 2872
            _seed_row(con, t, **extra)

    for t in _CONTEXT_OWNED_TABLES:
        if _table_exists(con, t):
            _seed_row(con, t, context_id=ctx)

    _seed_row(con, "pp_baskets", context_id=ctx, name="test basket")
    bid = con.execute("SELECT id FROM pp_baskets WHERE context_id=?", (ctx,)).fetchone()[0]
    _seed_row(con, "pp_basket_items", basket_id=bid)

    if _table_exists(con, "pp_markets"):
        _seed_row(con, "pp_markets", owner_kind="account", owner_id=ctx, name="my market")
        _seed_row(con, "pp_markets", owner_kind="group", owner_id=ctx, name="group market")

    _seed_row(con, "pp_bugs", context_id=ctx, character_id=char_ids[0],
              character_name=f"{name_prefix} 0", title="test bug", description="body")

    token = secrets.token_urlsafe(24)
    con.execute("INSERT INTO pp_sessions (token, character_id, context_id, created_at) VALUES (?,?,?,?)",
                (token, char_ids[0], ctx, datetime.now(timezone.utc).isoformat()))
    con.commit()
    con.close()
    return token, bid


def test_deletes_everything_keyed_to_the_account() -> bool:
    print(f"\n{'='*60}\n  Account deletion leaves nothing behind\n{'='*60}")
    ok = True
    _cleanup()
    _seed_account(MINE_CTX, [MINE_CID, MINE_CID2], "Mine")

    con = get_connection()
    seeded_char = {t: _count(con, t, "character_id IN (?,?)", (MINE_CID, MINE_CID2))
                   for t in _CHAR_OWNED_TABLES if _table_exists(con, t)}
    seeded_ctx = {t: _count(con, t, "context_id=?", (MINE_CTX,))
                  for t in _CONTEXT_OWNED_TABLES if _table_exists(con, t)}
    con.close()
    ok &= check(all(v > 0 for v in seeded_char.values()),
                f"seeded {len(seeded_char)} per-character tables (got {seeded_char})")
    ok &= check(all(v > 0 for v in seeded_ctx.values()),
                f"seeded {len(seeded_ctx)} context tables (got {seeded_ctx})")

    res = delete_account(context_id=MINE_CTX)
    ok &= check(res.get("deleted") is True, f"endpoint reports success (got {res})")

    con = get_connection()
    left_char = {t: n for t in seeded_char
                 if (n := _count(con, t, "character_id IN (?,?)", (MINE_CID, MINE_CID2)))}
    left_ctx = {t: n for t in seeded_ctx if (n := _count(con, t, "context_id=?", (MINE_CTX,)))}
    chars_left = _count(con, "pp_characters", "context_id=?", (MINE_CTX,))
    sess_left = _count(con, "pp_sessions", "context_id=?", (MINE_CTX,))
    ctx_left = con.execute("SELECT COUNT(*) FROM pp_user_contexts WHERE id=?", (MINE_CTX,)).fetchone()[0]
    con.close()

    ok &= check(not left_char, f"no per-character rows survive (leftovers: {left_char})")
    ok &= check(not left_ctx, f"no context-scoped rows survive (leftovers: {left_ctx})")
    ok &= check(chars_left == 0, "pp_characters rows deleted")
    ok &= check(sess_left == 0, "sessions deleted")
    ok &= check(ctx_left == 0, "the context row itself is deleted")
    return ok


def test_baskets_and_markets() -> bool:
    print(f"\n{'='*60}\n  Baskets cascade; group markets are not collateral damage\n{'='*60}")
    ok = True
    _cleanup()
    _, bid = _seed_account(MINE_CTX, [MINE_CID], "Mine")

    delete_account(context_id=MINE_CTX)

    con = get_connection()
    baskets = _count(con, "pp_baskets", "context_id=?", (MINE_CTX,))
    items = _count(con, "pp_basket_items", "basket_id=?", (bid,))
    acct_mkt = grp_mkt = None
    if _table_exists(con, "pp_markets"):
        acct_mkt = _count(con, "pp_markets", "owner_kind='account' AND owner_id=?", (MINE_CTX,))
        grp_mkt = _count(con, "pp_markets", "owner_kind='group' AND owner_id=?", (MINE_CTX,))
    con.close()

    ok &= check(baskets == 0, "the account's private baskets are deleted")
    ok &= check(items == 0, f"and their items cascade (got {items} orphaned)")
    if acct_mkt is not None:
        ok &= check(acct_mkt == 0, "the account's own followed markets are deleted")
        ok &= check(grp_mkt == 1,
                    f"group-level markets survive — they belong to the group, not the account (got {grp_mkt})")
    return ok


def test_bug_reports_are_anonymised_not_deleted() -> bool:
    print(f"\n{'='*60}\n  Bug reports survive, stripped of identity\n{'='*60}")
    ok = True
    _cleanup()
    _seed_account(MINE_CTX, [MINE_CID], "Mine")

    delete_account(context_id=MINE_CTX)

    con = get_connection()
    row = con.execute(
        "SELECT context_id, character_id, character_name, title FROM pp_bugs WHERE title=?",
        ("test bug",)).fetchone()
    con.close()
    ok &= check(row is not None, "the report itself is still there for admins to triage")
    if row:
        ok &= check(row[0] is None and row[1] is None,
                    f"context/character link removed (got context={row[0]}, char={row[1]})")
        ok &= check(row[2] == "(deleted account)",
                    f"reporter name replaced, not left in place (got {row[2]!r})")
    return ok


def test_other_accounts_are_untouched() -> bool:
    print(f"\n{'='*60}\n  A second account is completely unaffected\n{'='*60}")
    ok = True
    _cleanup()
    _seed_account(MINE_CTX, [MINE_CID], "Mine")
    _seed_account(OTHER_CTX, [OTHER_CID], "Theirs")

    con = get_connection()
    before_char = {t: _count(con, t, "character_id=?", (OTHER_CID,))
                   for t in _CHAR_OWNED_TABLES if _table_exists(con, t)}
    before_ctx = {t: _count(con, t, "context_id=?", (OTHER_CTX,))
                  for t in _CONTEXT_OWNED_TABLES if _table_exists(con, t)}
    con.close()

    delete_account(context_id=MINE_CTX)

    con = get_connection()
    after_char = {t: _count(con, t, "character_id=?", (OTHER_CID,)) for t in before_char}
    after_ctx = {t: _count(con, t, "context_id=?", (OTHER_CTX,)) for t in before_ctx}
    still_there = _count(con, "pp_characters", "context_id=?", (OTHER_CTX,))
    sess = _count(con, "pp_sessions", "context_id=?", (OTHER_CTX,))
    bug = _count(con, "pp_bugs", "context_id=?", (OTHER_CTX,))
    con.close()

    ok &= check(after_char == before_char, f"per-character rows unchanged ({before_char} -> {after_char})")
    ok &= check(after_ctx == before_ctx, f"context rows unchanged ({before_ctx} -> {after_ctx})")
    ok &= check(still_there == 1, "their character still exists")
    ok &= check(sess == 1, "their session is still valid")
    ok &= check(bug == 1, "their bug report is untouched, not anonymised")
    return ok


def test_live_endpoint_requires_a_session(base: str) -> bool:
    print(f"\n{'='*60}\n  Live DELETE /api/me requires a session\n{'='*60}")
    ok = True
    _cleanup()
    token, _ = _seed_account(MINE_CTX, [MINE_CID], "Mine")

    def _delete(cookie=None):
        req = urllib.request.Request(f"{base}/api/me", method="DELETE")
        if cookie:
            req.add_header("Cookie", f"pp_session={cookie}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, None
        except Exception as e:
            return None, str(e)

    status, _ = _delete()
    ok &= check(status == 401, f"anonymous DELETE /api/me is rejected 401 (got {status})")
    con = get_connection()
    ok &= check(_count(con, "pp_characters", "context_id=?", (MINE_CTX,)) == 1,
                "and the account is still intact")
    con.close()

    status, body = _delete(token)
    ok &= check(status == 200, f"own account deletes over HTTP (got {status})")
    ok &= check(bool(body) and body.get("deleted") is True, f"response confirms deletion (got {body})")

    status2, _ = _delete(token)
    ok &= check(status2 == 401, f"the session is dead afterwards (got {status2})")
    return ok


def main() -> int:
    base = "http://localhost:8000"
    for i, a in enumerate(sys.argv):
        if a == "--url" and i + 1 < len(sys.argv):
            base = sys.argv[i + 1].rstrip("/")
    ensure_char_tables()

    results = [
        test_deletes_everything_keyed_to_the_account(),
        test_baskets_and_markets(),
        test_bug_reports_are_anonymised_not_deleted(),
        test_other_accounts_are_untouched(),
        test_live_endpoint_requires_a_session(base),
    ]
    _cleanup()
    print(f"\n{'='*60}")
    if all(results):
        print("  ALL TESTS PASSED")
        return 0
    print(f"  {results.count(False)} of {len(results)} TEST GROUPS FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
