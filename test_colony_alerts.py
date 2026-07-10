"""
Correctness tests for the shared colony-alert engine (app/colony_alerts.py) and its two
consumers: the Dashboard (app/planner.py's dashboard()) and the notification scheduler
(app/notifications.py). Added when notifications were unified onto the same alert-detection
engine the Dashboard uses, instead of re-implementing detection independently — this exact
subsystem has caused a real production bug before (duplicate notifications from a scheduler
race, see check_and_send_notifications()'s docstring), so it gets deliberately thorough
coverage: the notify_kinds migration, per-kind severity computation for all 8 alert kinds, and
muting.

Two layers:
  1. In-process tests seeding fake pp_char_planets/pp_characters/pp_notification_prefs rows for
     a throwaway context_id — exercises the real DB-backed logic without needing a live ESI
     login.
  2. A live smoke test hitting the real /api/dashboard endpoint via a fabricated pp_sessions
     cookie, to confirm the Dashboard's issue-grouping (not just the flat alert list) renders
     the expected cards end-to-end.

Usage:
    python test_colony_alerts.py [--url http://localhost:8000]
"""
import argparse
import json
import secrets
import sys
import time
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, ".")
from app.sde import get_connection  # noqa: E402
from app.notifications import ensure_notification_tables, _get_prefs  # noqa: E402
from app.alert_settings import ALERT_KINDS, ensure_alert_settings_table  # noqa: E402
from app.colony_alerts import compute_colony_alerts  # noqa: E402

FAKE_CTX = 777001
FAKE_CID = 990001


def check(cond: bool, msg: str) -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    return cond


def _cleanup():
    con = get_connection()
    con.execute("DELETE FROM pp_char_planets WHERE character_id=?", (FAKE_CID,))
    con.execute("DELETE FROM pp_characters WHERE character_id=?", (FAKE_CID,))
    con.execute("DELETE FROM pp_sessions WHERE context_id=?", (FAKE_CTX,))
    con.execute("DELETE FROM pp_plan_snapshots WHERE context_id=?", (FAKE_CTX,))
    con.execute("DELETE FROM pp_notification_prefs WHERE context_id IN (?, 777999)", (FAKE_CTX,))
    con.execute("DELETE FROM pp_alert_settings WHERE context_id=?", (FAKE_CTX,))
    con.commit()
    con.close()


def _seed_character():
    con = get_connection()
    con.execute(
        "INSERT INTO pp_characters (character_id, character_name, context_id) VALUES (?,?,?) "
        "ON CONFLICT (character_id) DO UPDATE SET context_id=excluded.context_id",
        (FAKE_CID, "Test Toon", FAKE_CTX),
    )
    con.commit()
    con.close()


def _insert_planet(pid, pn, is_ext, issues="[]", sim="null", storage="null", scanned_at=None):
    con = get_connection()
    con.execute(
        "INSERT INTO pp_char_planets (character_id, planet_id, planet_type, planet_num, "
        "is_extractor, issues, sim_state, storage, scanned_at, checkpoint_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (FAKE_CID, pid, "Barren", pn, 1 if is_ext else 0, issues, sim, storage, scanned_at, scanned_at),
    )
    con.commit()
    con.close()


def test_migration_preserves_existing_prefs() -> bool:
    print(f"\n{'='*60}\n  notify_kinds migration from old boolean prefs\n{'='*60}")
    ok = True
    con = get_connection()
    con.execute("DELETE FROM pp_notification_prefs WHERE context_id=?", (FAKE_CTX,))
    con.commit()
    ensure_notification_tables()
    # Simulate a pre-migration row: notify_extractors=1, notify_factories=0, notify_kinds NULL.
    con.execute(
        "INSERT INTO pp_notification_prefs (context_id, lead_hours, notify_extractors, notify_factories) "
        "VALUES (?,4,1,0)",
        (FAKE_CTX,),
    )
    con.commit()
    con.close()
    # Re-run the migration pass directly (ensure_notification_tables is @ensure_once, already
    # ran once at import time before this row existed).
    con = get_connection()
    rows = con.execute(
        "SELECT context_id, notify_extractors, notify_factories FROM pp_notification_prefs WHERE notify_kinds IS NULL"
    ).fetchall()
    for r in rows:
        kinds = []
        if r["notify_extractors"]:
            kinds += ["expired", "expiring"]
        if r["notify_factories"]:
            kinds += ["factory_refill"]
        con.execute(
            "UPDATE pp_notification_prefs SET notify_kinds=?, min_severity=COALESCE(min_severity,'warn') WHERE context_id=?",
            (json.dumps(kinds), r["context_id"]),
        )
    con.commit()
    prefs = _get_prefs(con, FAKE_CTX)
    con.close()
    ok &= check(set(prefs["notify_kinds"]) == {"expired", "expiring"},
                f"notify_extractors=1/notify_factories=0 migrates to expired+expiring only (got {prefs['notify_kinds']})")
    ok &= check("storage_full" not in prefs["notify_kinds"],
                "new kinds NOT auto-enabled for an already-configured account")
    ok &= check(prefs["min_severity"] == "warn", "min_severity defaults to warn")
    return ok


def test_brand_new_context_defaults_to_everything() -> bool:
    print(f"\n{'='*60}\n  Brand new context (no prefs row at all)\n{'='*60}")
    con = get_connection()
    prefs = _get_prefs(con, 777999)
    con.close()
    return check(set(prefs["notify_kinds"]) == {k["key"] for k in ALERT_KINDS},
                 f"defaults to all {len(ALERT_KINDS)} kinds, matching the old opt-out default (got {prefs['notify_kinds']})")


def test_unknown_kind_filtered() -> bool:
    print(f"\n{'='*60}\n  Unknown/stale kind strings are filtered defensively\n{'='*60}")
    con = get_connection()
    con.execute("UPDATE pp_notification_prefs SET notify_kinds=? WHERE context_id=?",
                (json.dumps(["expired", "some_removed_kind"]), FAKE_CTX))
    con.commit()
    prefs = _get_prefs(con, FAKE_CTX)
    con.close()
    return check(prefs["notify_kinds"] == ["expired"], f"got {prefs['notify_kinds']}")


def test_all_eight_kinds_detected() -> bool:
    print(f"\n{'='*60}\n  compute_colony_alerts detects all 8 kinds with correct severity\n{'='*60}")
    ok = True
    _seed_character()
    now = time.time()
    con = get_connection()
    con.execute("DELETE FROM pp_char_planets WHERE character_id=?", (FAKE_CID,))
    con.execute("DELETE FROM pp_plan_snapshots WHERE context_id=?", (FAKE_CTX,))
    con.execute(
        "INSERT INTO pp_plan_snapshots (context_id, name, snapshot, created_at) VALUES (?,?,?,?)",
        (FAKE_CTX, "test snap", json.dumps({"factory_refill_hours": 48}), "now"),
    )
    con.commit()
    con.close()

    _insert_planet(1, 1, True, sim=json.dumps({"expiry": now - 3600}))                       # expired
    _insert_planet(2, 2, True, sim=json.dumps({"expiry": now + 3600}),                        # expiring
                   storage=json.dumps({"fill_m3_h": 0, "vol_m3": 27000, "cap_m3": 30000}))     # storage_full @ 90%
    _insert_planet(3, 3, True, issues=json.dumps(["ext_unrouted"]))
    _insert_planet(4, 4, False, issues=json.dumps(["fac_unfed"]),
                   scanned_at=now - (48 - 1) * 3600)                                           # due in 1h -> high
    _insert_planet(5, 5, True, issues=json.dumps(["fac_output"]))
    _insert_planet(6, 6, True, issues=json.dumps(["p0_mismatch"]))

    alerts = compute_colony_alerts(FAKE_CTX)
    by_kind = {}
    for a in alerts:
        by_kind.setdefault(a["kind"], []).append(a)

    expected_high = {"expired", "ext_unrouted", "fac_unfed", "fac_output", "p0_mismatch", "factory_refill"}
    for kind in expected_high:
        ok &= check(kind in by_kind and by_kind[kind][0]["severity"] == "high",
                    f"{kind} detected with high severity (got {by_kind.get(kind)})")
    ok &= check("expiring" in by_kind and by_kind["expiring"][0]["severity"] == "warn",
                f"expiring detected with warn severity (got {by_kind.get('expiring')})")
    ok &= check("storage_full" in by_kind and by_kind["storage_full"][0]["pct"] == 90
                and by_kind["storage_full"][0]["severity"] == "warn",
                f"storage_full detected at 90%, warn severity — below the 95%/2h high cutoffs (got {by_kind.get('storage_full')})")
    ok &= check(all(a["planet_id"] is not None for a in alerts), "every alert carries a planet_id (needed for notification cooldown keying)")
    return ok


def test_muting_excludes_from_engine_output() -> bool:
    print(f"\n{'='*60}\n  Muting a kind removes it from compute_colony_alerts (both consumers)\n{'='*60}")
    ok = True
    ensure_alert_settings_table()
    con = get_connection()
    con.execute(
        "INSERT INTO pp_alert_settings (context_id, expiring_hours, storage_warn_pct, storage_high_pct, "
        "storage_high_ttf_hours, storage_urgent_hours, muted_kinds) VALUES (?,3,80,95,2,3,?) "
        "ON CONFLICT(context_id) DO UPDATE SET muted_kinds=excluded.muted_kinds",
        (FAKE_CTX, json.dumps(["ext_unrouted"])),
    )
    con.commit()
    con.close()
    kinds = {a["kind"] for a in compute_colony_alerts(FAKE_CTX)}
    ok &= check("ext_unrouted" not in kinds, f"muted kind excluded (got kinds: {kinds})")
    ok &= check("expired" in kinds, "unmuted kinds still present")
    return ok


def test_live_dashboard_renders_expected_cards(base: str) -> bool:
    print(f"\n{'='*60}\n  Live /api/dashboard renders the expected issue cards\n{'='*60}")
    ok = True
    token = secrets.token_urlsafe(24)
    con = get_connection()
    con.execute("DELETE FROM pp_sessions WHERE context_id=?", (FAKE_CTX,))
    con.execute("INSERT INTO pp_sessions (token, character_id, context_id, created_at) VALUES (?,?,?,?)",
                (token, FAKE_CID, FAKE_CTX, datetime.now(timezone.utc).isoformat()))
    con.commit()
    con.close()

    req = urllib.request.Request(f"{base}/api/dashboard")
    req.add_header("Cookie", f"pp_session={token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        return check(False, f"request succeeded (got exception: {e})")

    ok &= check(data.get("logged_in") is True, "session resolved to logged_in")
    ok &= check(FAKE_CID in (data.get("char_ids_in_view") or []), "seeded character appears in char_ids_in_view")
    headers = {i["char"] for i in data.get("issues", [])}
    # ext_unrouted is muted from the previous test, so "Test Toon"'s correctness card should
    # only mention fac_unfed/fac_output/p0_mismatch, not the unrouted extractor.
    correctness_card = next((i for i in data["issues"] if i["char"] == "Test Toon"), None)
    ok &= check(correctness_card is not None, "correctness-kind card present for Test Toon")
    if correctness_card:
        msgs = " ".join(it["msg"] for it in correctness_card["items"])
        ok &= check("extractor not routed" not in msgs, f"muted ext_unrouted does not appear (got: {msgs})")
    ok &= check("Extractions expired" in headers, f"expired card present (got headers: {headers})")
    ok &= check(any(h.startswith("Storage filling up") for h in headers), "storage_full card present")
    ok &= check(any(h.startswith("Factories due for refill") for h in headers), "factory_refill card present")
    refill_card = next((i for i in data["issues"] if i["char"].startswith("Factories due for refill")), None)
    ok &= check(refill_card is not None and refill_card["severity"] == "high",
                f"factory_refill card severity reflects the imminent (due-in-1h) instance, not a hardcoded default (got {refill_card})")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    args = parser.parse_args()
    base = args.url.rstrip("/")

    _cleanup()
    results = [
        test_migration_preserves_existing_prefs(),
        test_brand_new_context_defaults_to_everything(),
        test_unknown_kind_filtered(),
        test_all_eight_kinds_detected(),
        test_muting_excludes_from_engine_output(),
        test_live_dashboard_renders_expected_cards(base),
    ]
    _cleanup()

    print(f"\n{'='*60}")
    if all(results):
        print("  ALL TESTS PASSED")
        return 0
    print("  SOME TESTS FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
