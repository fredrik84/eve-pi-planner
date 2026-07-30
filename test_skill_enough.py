"""
"Already enough skill" half of /api/skill-roi.

PI advice defaults to "train everything to V". A command centre is sized by CPU and power grid,
and plenty of colonies never come close to the level-V budget — so a character whose colonies all
run a level-III command centre should be told that, not told to grind a rank-4 skill. These seed a
throwaway context with colonies that DON'T need the character's trained level and assert the
endpoint says so (and, just as importantly, stays quiet when the level really is needed).

Seeds real rows + a fabricated pp_sessions cookie, same approach as test_alerts.py.
Run inside the container:
    docker exec eve-pi-planner-web-1 python3 test_skill_enough.py --url http://localhost:8000
"""

import argparse
import json
import secrets
import sys
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, ".")
from app.sde import get_connection  # noqa: E402

FAKE_CTX = 777301
UNDER = 990301        # CCU 5 trained, colonies deployed at level 3
MATCHED = 990302      # CCU 5 trained, colonies deployed at level 5
IDLE = 990303         # IC 5 trained (6 slots), only 2 colonies deployed
FAKE_CIDS = (UNDER, MATCHED, IDLE)


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    return bool(cond)


def _cleanup():
    con = get_connection()
    for cid in FAKE_CIDS:
        con.execute("DELETE FROM pp_char_planets WHERE character_id=?", (cid,))
        con.execute("DELETE FROM pp_characters WHERE character_id=?", (cid,))
    con.execute("DELETE FROM pp_sessions WHERE context_id=?", (FAKE_CTX,))
    con.commit()
    con.close()


def _seed():
    con = get_connection()
    for cid, name, ccu, ic in ((UNDER, "Enough Toon", 5, 5),
                               (MATCHED, "Maxed Toon", 5, 5),
                               (IDLE, "Idle Toon", 5, 5)):
        con.execute(
            "INSERT INTO pp_characters (character_id, character_name, context_id, "
            " command_center_upgrades, interplanetary_consolidation) VALUES (?,?,?,?,?) "
            "ON CONFLICT (character_id) DO UPDATE SET context_id=excluded.context_id, "
            " command_center_upgrades=excluded.command_center_upgrades, "
            " interplanetary_consolidation=excluded.interplanetary_consolidation",
            (cid, name, FAKE_CTX, ccu, ic))
    # Colonies. upgrade_level is the level the command centre is ACTUALLY running at in-game.
    plan = [(UNDER, 3), (UNDER, 3), (UNDER, 2),      # highest deployed = 3, trained 5
            (MATCHED, 5), (MATCHED, 5),              # deployed = trained, nothing to say
            (IDLE, 5), (IDLE, 5)]                    # 2 of 6 slots used
    pid = 60003100
    for cid, lvl in plan:
        pid += 1
        con.execute(
            "INSERT INTO pp_char_planets (character_id, planet_id, planet_type, planet_num, "
            " is_extractor, upgrade_level, products) VALUES (?,?,?,?,?,?,?)",
            (cid, pid, "Barren", pid % 10, 1, lvl, "[]"))
    con.commit()
    con.close()


def _session_token():
    token = secrets.token_urlsafe(24)
    con = get_connection()
    con.execute("DELETE FROM pp_sessions WHERE context_id=?", (FAKE_CTX,))
    con.execute("INSERT INTO pp_sessions (token, character_id, context_id, created_at) VALUES (?,?,?,?)",
                (token, UNDER, FAKE_CTX, datetime.now(timezone.utc).isoformat()))
    con.commit()
    con.close()
    return token


def _get(base, token):
    req = urllib.request.Request(f"{base}/api/skill-roi")
    req.add_header("Cookie", f"pp_session={token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def main(base):
    ok = True
    _cleanup()
    _seed()
    data = _get(base, _session_token())
    enough = data.get("enough")
    ok &= check(isinstance(enough, list), "response carries an 'enough' list")
    enough = enough or []
    by_char = {}
    for e in enough:
        by_char.setdefault(e["char"], []).append(e)

    print("\n  Character whose colonies run below their trained level:")
    ccu = next((e for e in by_char.get("Enough Toon", []) if e["skill"] == "Command Center Upgrades"), None)
    ok &= check(ccu is not None, "Enough Toon is told CCU is already sufficient")
    if ccu:
        ok &= check(ccu["have_lvl"] == 5 and ccu["need_lvl"] == 3,
                    f"reports the deployed level, not the trained one (need {ccu['need_lvl']} of {ccu['have_lvl']})")
        ok &= check(ccu.get("basis") == "deployed", "based on observed colonies, not a model")

    print("\n  Character actually using their level (must stay quiet):")
    ok &= check(not any(e["skill"] == "Command Center Upgrades" for e in by_char.get("Maxed Toon", [])),
                "Maxed Toon gets no 'already enough' CCU advice")

    print("\n  Trained planet slots sitting empty:")
    ic = next((e for e in by_char.get("Idle Toon", []) if e["skill"] == "Interplanetary Consolidation"), None)
    ok &= check(ic is not None, "Idle Toon is told about undeployed planet slots")
    if ic:
        ok &= check(ic.get("free_slots") == 4, f"counts the 4 empty slots (got {ic.get('free_slots')})")
    # ...and must NOT also be told to train IC for a 7th slot while 4 sit empty.
    sugs = [s for s in (data.get("suggestions") or [])
            if s["char"] == "Idle Toon" and s["skill"] == "Interplanetary Consolidation"]
    ok &= check(not sugs, "Idle Toon is NOT told to train IC while slots are undeployed")

    _cleanup()
    print("\n" + ("  ALL TESTS PASSED" if ok else "  FAILURES ABOVE"))
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    sys.exit(main(ap.parse_args().url.rstrip("/")))
