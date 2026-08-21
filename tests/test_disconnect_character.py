"""
Correctness tests for disconnecting a character (DELETE /api/characters/{id}).

This endpoint deletes across ~10 tables and mutates three more, so the failure modes worth
guarding are: leaving orphaned per-character rows behind (the bug this suite was written for —
it used to clear only pp_characters + pp_char_planets), deleting somebody ELSE's character, and
destroying account-level history that isn't the character's to take with it.

Two layers:
  1. In-process tests calling remove_character(character_id, context_id) directly — the endpoint
     takes context_id as a parameter, so the whole body is reachable without HTTP.
  2. A live HTTP test through the real /api/characters/{id} route with a fabricated pp_sessions
     cookie, which is the only way to exercise the require_context gate itself.

Run inside the container (needs the app's DB):
    docker cp tests/test_disconnect_character.py <container>:/srv/app/tests/ && \
      docker exec <container> python3 tests/test_disconnect_character.py [--url http://localhost:8000]
"""
import json
import secrets
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

sys.path.insert(0, ".")
from app.sde import get_connection  # noqa: E402
from app.esi import ensure_char_tables  # noqa: E402
from app.esi_data import remove_character, _CHAR_OWNED_TABLES, _table_exists  # noqa: E402

FAKE_CTX = 778001          # the account under test
FAKE_CID = 991001          # the character being disconnected
KEEP_CID = 991002          # a second character on the SAME account — must survive
OTHER_CTX = 778002         # a different account entirely
OTHER_CID = 991003         # its character — must be untouchable from FAKE_CTX


def check(cond: bool, msg: str) -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    return bool(cond)


def _cleanup():
    con = get_connection()
    for cid in (FAKE_CID, KEEP_CID, OTHER_CID):
        for t in _CHAR_OWNED_TABLES:
            if _table_exists(con, t):
                con.execute(f"DELETE FROM {t} WHERE character_id=?", (cid,))
        con.execute("DELETE FROM pp_characters WHERE character_id=?", (cid,))
    for ctx in (FAKE_CTX, OTHER_CTX):
        con.execute("DELETE FROM pp_sessions WHERE context_id=?", (ctx,))
        con.execute("DELETE FROM pp_profiles WHERE context_id=?", (ctx,))
        con.execute("DELETE FROM pp_bugs WHERE context_id=?", (ctx,))
        if _table_exists(con, "pp_market_config"):
            con.execute("DELETE FROM pp_market_config WHERE context_id=?", (ctx,))
        if _table_exists(con, "pp_reaction_completions"):
            con.execute("DELETE FROM pp_reaction_completions WHERE context_id=?", (ctx,))
    con.commit()
    con.close()


def _seed_char(cid: int, ctx: int, name: str):
    con = get_connection()
    con.execute(
        "INSERT INTO pp_characters (character_id, character_name, context_id, refresh_token) "
        "VALUES (?,?,?,?) ON CONFLICT (character_id) DO UPDATE SET context_id=excluded.context_id",
        (cid, name, ctx, ""),   # empty refresh_token: revoke_refresh_token() no-ops, no SSO call
    )
    con.commit()
    con.close()


def _seed_char_data(cid: int):
    """One row in every per-character table that exists, so we can prove each gets cleared."""
    con = get_connection()
    seeded = []
    for t in _CHAR_OWNED_TABLES:
        if not _table_exists(con, t):
            continue
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({t})")} if _is_sqlite(con) else None
        try:
            if t == "pp_char_planets":
                con.execute("INSERT INTO pp_char_planets (character_id, planet_id, planet_type) "
                            "VALUES (?,?,?)", (cid, 4001, "Barren"))
            elif t == "pp_colony_yield":
                con.execute("INSERT INTO pp_colony_yield (character_id, planet_id, install_ts) "
                            "VALUES (?,?,?)", (cid, 4001, 1.0))
            elif t == "pp_colony_flags":
                con.execute("INSERT INTO pp_colony_flags (context_id, character_id, planet_id) "
                            "VALUES (?,?,?)", (FAKE_CTX, cid, 4001))
            elif t == "pp_plan_config":
                con.execute("INSERT INTO pp_plan_config (character_id, product_type_id, planet_limit) "
                            "VALUES (?,?,?)", (cid, 2872, 6))
            elif t == "pp_reaction_assignments":
                con.execute("INSERT INTO pp_reaction_assignments (character_id, type_id, name, runs, "
                            "input_cost, reward, created_at) VALUES (?,?,?,?,?,?,?)",
                            (cid, 16671, "Test Reaction", 1, 0.0, 0.0, 1.0))
            else:
                # pp_char_assets / _blueprints / _industry_jobs / _manufacturing_jobs all share
                # the (character_id PK, <name>_json, fetched_at) shape.
                jcol = next(c for c in (cols or []) if c.endswith("_json"))
                con.execute(f"INSERT INTO {t} (character_id, {jcol}) VALUES (?,?)", (cid, "[]"))
            seeded.append(t)
        except Exception as e:
            print(f"    (skipped seeding {t}: {e})")
    con.commit()
    con.close()
    return seeded


def _is_sqlite(con) -> bool:
    return con.__class__.__module__.startswith("sqlite3")


def _rows_for(cid: int, tables) -> dict:
    con = get_connection()
    out = {}
    for t in tables:
        out[t] = con.execute(f"SELECT COUNT(*) FROM {t} WHERE character_id=?", (cid,)).fetchone()[0]
    con.close()
    return out


def test_deletes_every_per_character_table() -> bool:
    print(f"\n{'='*60}\n  Disconnect clears every per-character table\n{'='*60}")
    ok = True
    _cleanup()
    _seed_char(FAKE_CID, FAKE_CTX, "Doomed Toon")
    _seed_char(KEEP_CID, FAKE_CTX, "Keeper Toon")
    seeded = _seed_char_data(FAKE_CID)
    _seed_char_data(KEEP_CID)

    before = _rows_for(FAKE_CID, seeded)
    ok &= check(all(v > 0 for v in before.values()),
                f"seeded at least one row in each of {len(seeded)} tables (got {before})")

    res = remove_character(FAKE_CID, context_id=FAKE_CTX)
    ok &= check(res.get("removed") == FAKE_CID, f"endpoint reports removal (got {res})")

    after = _rows_for(FAKE_CID, seeded)
    leftovers = {t: n for t, n in after.items() if n}
    ok &= check(not leftovers, f"no orphaned rows in any per-character table (leftovers: {leftovers})")

    con = get_connection()
    gone = con.execute("SELECT COUNT(*) FROM pp_characters WHERE character_id=?", (FAKE_CID,)).fetchone()[0]
    con.close()
    ok &= check(gone == 0, "pp_characters row (and its stored tokens) deleted")

    # The other character on the same account must be entirely untouched.
    kept = _rows_for(KEEP_CID, seeded)
    ok &= check(all(v > 0 for v in kept.values()),
                f"the account's OTHER character keeps all its rows (got {kept})")
    return ok


def test_cannot_disconnect_another_accounts_character() -> bool:
    print(f"\n{'='*60}\n  A character on another account is not removable\n{'='*60}")
    ok = True
    _cleanup()
    _seed_char(FAKE_CID, FAKE_CTX, "Mine")
    _seed_char(OTHER_CID, OTHER_CTX, "Someone Else")
    seeded = _seed_char_data(OTHER_CID)

    from fastapi import HTTPException
    try:
        remove_character(OTHER_CID, context_id=FAKE_CTX)
        ok &= check(False, "expected HTTPException, got a successful delete")
    except HTTPException as e:
        ok &= check(e.status_code == 404, f"raises 404, not a silent no-op (got {e.status_code})")

    con = get_connection()
    still = con.execute("SELECT COUNT(*) FROM pp_characters WHERE character_id=?", (OTHER_CID,)).fetchone()[0]
    con.close()
    ok &= check(still == 1, "the other account's character row survives")
    after = _rows_for(OTHER_CID, seeded)
    ok &= check(all(v > 0 for v in after.values()), f"and so does all its data (got {after})")
    return ok


def test_clears_references_to_the_character() -> bool:
    print(f"\n{'='*60}\n  Dangling references are cleared (market reader, saved plans)\n{'='*60}")
    ok = True
    _cleanup()
    _seed_char(FAKE_CID, FAKE_CTX, "Doomed Toon")
    _seed_char(KEEP_CID, FAKE_CTX, "Keeper Toon")

    con = get_connection()
    has_mc = _table_exists(con, "pp_market_config")
    if has_mc:
        con.execute("INSERT INTO pp_market_config (context_id, market_character_id) VALUES (?,?)",
                    (FAKE_CTX, FAKE_CID))
    con.execute(
        "INSERT INTO pp_profiles (context_id, name, type_id, factory_character_ids) VALUES (?,?,?,?)",
        (FAKE_CTX, "test profile", 2872, json.dumps([FAKE_CID, KEEP_CID])),
    )
    con.commit()
    con.close()

    remove_character(FAKE_CID, context_id=FAKE_CTX)

    con = get_connection()
    if has_mc:
        mc = con.execute("SELECT market_character_id FROM pp_market_config WHERE context_id=?",
                         (FAKE_CTX,)).fetchone()
        ok &= check(mc is not None and mc[0] is None,
                    f"designated market character cleared, not left dangling (got {mc and mc[0]})")
    fc = con.execute("SELECT factory_character_ids FROM pp_profiles WHERE context_id=?",
                     (FAKE_CTX,)).fetchone()
    con.close()
    ids = json.loads(fc[0]) if fc else None
    ok &= check(ids == [KEEP_CID],
                f"saved plan's factory-character list drops the removed id, keeps the rest (got {ids})")
    return ok


def test_session_is_repointed_then_ended() -> bool:
    print(f"\n{'='*60}\n  Disconnecting the logged-in character doesn't log you out prematurely\n{'='*60}")
    ok = True
    _cleanup()
    _seed_char(FAKE_CID, FAKE_CTX, "Doomed Toon")
    _seed_char(KEEP_CID, FAKE_CTX, "Keeper Toon")

    token = secrets.token_urlsafe(24)
    con = get_connection()
    con.execute("INSERT INTO pp_sessions (token, character_id, context_id, created_at) VALUES (?,?,?,?)",
                (token, FAKE_CID, FAKE_CTX, datetime.now(timezone.utc).isoformat()))
    con.commit()
    con.close()

    res = remove_character(FAKE_CID, context_id=FAKE_CTX)
    con = get_connection()
    sess = con.execute("SELECT character_id FROM pp_sessions WHERE token=?", (token,)).fetchone()
    con.close()
    ok &= check(sess is not None and sess[0] == KEEP_CID,
                f"session re-pointed to the surviving character, still logged in (got {sess and sess[0]})")
    ok &= check(res.get("logged_out") is False, f"and reports logged_out=False (got {res.get('logged_out')})")

    # Now remove the last character — the session has nowhere to go and must end.
    res2 = remove_character(KEEP_CID, context_id=FAKE_CTX)
    con = get_connection()
    sess2 = con.execute("SELECT character_id FROM pp_sessions WHERE token=?", (token,)).fetchone()
    con.close()
    ok &= check(sess2 is None, f"removing the LAST character ends the session (got {sess2})")
    ok &= check(res2.get("logged_out") is True, f"and reports logged_out=True (got {res2.get('logged_out')})")
    return ok


def test_account_history_survives() -> bool:
    print(f"\n{'='*60}\n  Account-level history is NOT collateral damage\n{'='*60}")
    ok = True
    _cleanup()
    _seed_char(FAKE_CID, FAKE_CTX, "Doomed Toon")

    con = get_connection()
    con.execute(
        "INSERT INTO pp_bugs (context_id, character_id, character_name, title, description) "
        "VALUES (?,?,?,?,?)",
        (FAKE_CTX, FAKE_CID, "Doomed Toon", "test bug", "body"),
    )
    has_rc = _table_exists(con, "pp_reaction_completions")
    if has_rc:
        con.execute(
            "INSERT INTO pp_reaction_completions (job_id, context_id, character_id, product_type_id, "
            "runs, net_profit, completed_at) VALUES (?,?,?,?,?,?,?)",
            (55501, FAKE_CTX, FAKE_CID, 16671, 1, 1234.0, 1.0))
    con.commit()
    con.close()

    remove_character(FAKE_CID, context_id=FAKE_CTX)

    con = get_connection()
    bug = con.execute("SELECT character_name FROM pp_bugs WHERE context_id=?", (FAKE_CTX,)).fetchone()
    ok_rc = True
    if has_rc:
        n = con.execute("SELECT COUNT(*) FROM pp_reaction_completions WHERE context_id=?",
                        (FAKE_CTX,)).fetchone()[0]
        ok_rc = n == 1
    con.close()
    ok &= check(bug is not None and bug[0] == "Doomed Toon",
                "the bug report survives, still naming the character (admin support record)")
    if has_rc:
        ok &= check(ok_rc, "the account's earnings ledger is not rewritten by disconnecting a character")
    return ok


def test_live_endpoint_requires_own_session(base: str) -> bool:
    print(f"\n{'='*60}\n  Live DELETE /api/characters/{{id}} enforces require_context\n{'='*60}")
    ok = True
    _cleanup()
    _seed_char(FAKE_CID, FAKE_CTX, "Doomed Toon")
    _seed_char(OTHER_CID, OTHER_CTX, "Someone Else")

    def _delete(cid, cookie=None):
        req = urllib.request.Request(f"{base}/api/characters/{cid}", method="DELETE")
        if cookie:
            req.add_header("Cookie", f"pp_session={cookie}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, None
        except Exception as e:
            return None, str(e)

    status, _ = _delete(FAKE_CID)
    ok &= check(status == 401, f"anonymous DELETE is rejected 401 (got {status})")

    token = secrets.token_urlsafe(24)
    con = get_connection()
    con.execute("INSERT INTO pp_sessions (token, character_id, context_id, created_at) VALUES (?,?,?,?)",
                (token, FAKE_CID, FAKE_CTX, datetime.now(timezone.utc).isoformat()))
    con.commit()
    con.close()

    status, _ = _delete(OTHER_CID, token)
    ok &= check(status == 404, f"a logged-in user cannot delete another account's character (got {status})")
    con = get_connection()
    still = con.execute("SELECT COUNT(*) FROM pp_characters WHERE character_id=?", (OTHER_CID,)).fetchone()[0]
    con.close()
    ok &= check(still == 1, "the other account's character is still there")

    status, body = _delete(FAKE_CID, token)
    ok &= check(status == 200, f"own character deletes successfully over HTTP (got {status})")
    ok &= check(bool(body) and body.get("name") == "Doomed Toon",
                f"response names the disconnected character (got {body})")
    return ok


def main() -> int:
    base = "http://localhost:8000"
    for i, a in enumerate(sys.argv):
        if a == "--url" and i + 1 < len(sys.argv):
            base = sys.argv[i + 1].rstrip("/")
    ensure_char_tables()

    results = [
        test_deletes_every_per_character_table(),
        test_cannot_disconnect_another_accounts_character(),
        test_clears_references_to_the_character(),
        test_session_is_repointed_then_ended(),
        test_account_history_survives(),
        test_live_endpoint_requires_own_session(base),
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
