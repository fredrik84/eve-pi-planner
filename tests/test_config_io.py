#!/usr/bin/env python3
"""Config export/import (TODO §18b) — the file is portable, and a bad one changes nothing.

Export/import is a serialiser over the readers and writers that already exist, so the things worth
pinning are not "does it save a number" but the four ways a portable config quietly goes wrong:

  * **an id that is not portable.** Build pins are stored as `s:<pp_markets row id>` — the account's
    own primary key. Written into another account verbatim they would point at whatever row happens
    to hold that id, which is a plan silently installing jobs in the wrong building. They travel as
    `location_id` and are re-resolved on the way in.
  * **a half-applied import.** The sections interlock, and a user who has just been told "imported"
    cannot tell which half took. The whole document validates before anything is written, and every
    problem is reported at once rather than one round-trip at a time.
  * **a round trip that is not one.** The four rig-family columns are JSON TEXT that the reader hands
    back parsed; writing the list straight back stores the string "['capital']" and the structure
    silently stops covering anything.
  * **importing twice.** Structures match on location and placeholders on name, so a second import is
    the same statement made twice, not a doubled roster.

Also pinned: a section whose feature flag is off is REPORTED as skipped, never silently dropped —
the failure `apply_patch` exists to prevent, reached from the import side.

In-process; run inside the container against a NON-PROD database.

    docker compose cp tests/test_config_io.py web:/srv/app/tests/ && \
      docker compose exec web python3 tests/test_config_io.py
"""
import copy
import json
import sys

sys.path.insert(0, ".")

_fails = []
CTX_A = 717171          # the account that exports
CTX_B = 717172          # a different account, importing A's file
LOC_1 = 9000000001      # POSITIVE, i.e. ESI-shaped. A negative id is a HAND-DESCRIBED structure
LOC_2 = 9000000002      # and is gated separately — see test_manual_structures_gated.
LOC_MANUAL = -9000001
LOC_REGION = 9000000009  # a followed REGION market, which replace_structures must never touch


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


# ── Fixtures ──────────────────────────────────────────────────────────────────────────────────

def _wipe(ctx: int) -> None:
    from app.db import get_connection
    con = get_connection()
    try:
        for sql in ("DELETE FROM pp_markets WHERE owner_kind='account' AND owner_id=?",
                    "DELETE FROM pp_industry_settings WHERE context_id=?",
                    "DELETE FROM pp_account_reaction_settings WHERE context_id=?",
                    "DELETE FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=1"):
            try:
                con.execute(sql, (ctx,))
            except Exception:
                pass
        con.commit()
    finally:
        con.close()


def _seed(ctx: int) -> None:
    """An account with two structures, a pin onto the SECOND of them, and a placeholder.

    The pin is deliberately on the second row: a bug that dropped the mapping and wrote the raw id
    through would still land on the right building if there were only one, or if the pin named the
    first row of both accounts."""
    from app.db import get_connection
    from app.markets import ensure_markets_table
    from app.industry.settings import set_build_pins, save_settings
    ensure_markets_table()
    con = get_connection()
    try:
        for i, (loc, name) in enumerate(((LOC_1, "Test Raitaru"), (LOC_2, "Test Sotiyo"))):
            con.execute(
                "INSERT INTO pp_markets (owner_kind, owner_id, kind, location_id, name, priority, "
                "active, build_mfg, build_rx, hull, security, me_rig, te_rig, me_rig_groups) "
                "VALUES ('account',?,'structure',?,?,?,1,1,0,?,'highsec',?,?,?)",
                (ctx, loc, name, i, "raitaru" if i == 0 else "sotiyo", 1 if i else 2, 1,
                 json.dumps(["capital_ship"] if i else [])))
        con.execute(
            "INSERT INTO pp_characters (character_id, character_name, "
            "interplanetary_consolidation, command_center_upgrades, context_id, "
            "dummy_mfg_slots, dummy_rx_slots, is_dummy) VALUES (?,?,?,?,?,?,?,1)",
            (-8888881, "Config Test Alt", 3, 4, ctx, 7, 5))
        con.commit()
    finally:
        con.close()
    save_settings(ctx, {"margin_pct": 12.5, "marginal_pct": 4.0, "facility_id": "sotiyo"})
    rows = _rows(ctx)
    set_build_pins(ctx, {"capital_ship": f"s:{rows[LOC_2]}"})


def _rows(ctx: int) -> dict:
    """location_id -> pp_markets row id, for this account's STRUCTURES."""
    from app.db import get_connection
    con = get_connection()
    try:
        return {r["location_id"]: r["id"] for r in con.execute(
            "SELECT id, location_id FROM pp_markets "
            "WHERE owner_kind='account' AND owner_id=? AND kind='structure'", (ctx,)).fetchall()}
    finally:
        con.close()


def _market_rows(ctx: int) -> list:
    """EVERY pp_markets row for this account, structures and followed regions alike, with the two
    columns the checks below care about."""
    from app.db import get_connection
    con = get_connection()
    try:
        return [dict(r) for r in con.execute(
            "SELECT kind, location_id, name, active FROM pp_markets "
            "WHERE owner_kind='account' AND owner_id=? ORDER BY id", (ctx,)).fetchall()]
    finally:
        con.close()


def _add_market(ctx: int, kind: str, loc: int, name: str, active: int = 1) -> None:
    from app.db import get_connection
    from app.markets import ensure_markets_table
    ensure_markets_table()
    con = get_connection()
    try:
        con.execute("INSERT INTO pp_markets (owner_kind, owner_id, kind, location_id, name, "
                    "priority, active) VALUES ('account',?,?,?,?,99,?)",
                    (ctx, kind, loc, name, active))
        con.commit()
    finally:
        con.close()


def _state(ctx: int) -> dict:
    """The account's configuration as a comparable value. `exported_at` is dropped — it is a fact
    about the download, not about the configuration, and two exports a millisecond apart differ by
    it alone."""
    from app.config_io import export_config
    doc = export_config(ctx)
    doc.pop("exported_at", None)
    return doc


def _feature_states(keys) -> dict:
    """Read the rung each of these features is parked on, so it can be put back exactly."""
    from app.db import get_connection
    from app.features import ensure_features_table
    ensure_features_table()
    con = get_connection()
    try:
        rows = {r["key"]: r["state"] for r in con.execute("SELECT key, state FROM pp_features")}
    finally:
        con.close()
    return {k: rows.get(k) for k in keys}


def _set_features(states: dict) -> None:
    """Force these features onto a rung for the duration of a check — and only that.

    The gated sections are the ones worth testing (a pin that is never resolved is the whole bug
    this module guards), and their rung is runtime state an admin moves, so a test that merely
    OBSERVED it would cover the interesting path only by luck — which is exactly what happened on
    the first run here. Every value read by `_feature_states` is restored afterwards, including
    "there was no row", which is the state a fresh database is in."""
    from app.db import get_connection
    from app.features import ensure_features_table
    ensure_features_table()
    con = get_connection()
    try:
        for key, state in states.items():
            if state is None:
                con.execute("DELETE FROM pp_features WHERE key=?", (key,))
            else:
                con.execute(
                    "INSERT INTO pp_features (key, enabled, state, updated_at) VALUES (?,?,?,'test') "
                    "ON CONFLICT(key) DO UPDATE SET enabled=excluded.enabled, state=excluded.state",
                    (key, 1 if state == "public" else 0, state))
        con.commit()
    finally:
        con.close()


# Every flag the import's sections hang off. Turned public around the round trip so the gated half
# is actually exercised, then restored.
GATED = ("industry_rig_routing", "industry_reaction_policy", "industry_blacklist",
         "industry_job_length_policy", "industry_per_order_plans", "industry_plan_sources")


def _pins(ctx: int) -> dict:
    from app.cache import begin_request_memo
    from app.industry.settings import get_build_pins
    begin_request_memo()          # a fresh request, so nothing is read through a stale memo
    return get_build_pins(ctx)


# ── Checks ────────────────────────────────────────────────────────────────────────────────────

def test_validation() -> None:
    from app.config_io import _validate, CONFIG_KIND, CONFIG_VERSION
    print("\na file is checked whole, and every problem is reported at once:")
    check(_validate({"kind": CONFIG_KIND, "version": CONFIG_VERSION}) == [],
          "a minimal well-formed document has no problems")
    check(_validate("not a document"), "a non-object is refused")
    check(any("eve-pi-planner" in p for p in _validate({"kind": "ravworks", "version": 1})),
          "a file from another tool is refused by kind")
    newer = _validate({"kind": CONFIG_KIND, "version": CONFIG_VERSION + 1})
    check(any("newer version" in p for p in newer),
          "a file from a newer deploy is refused, saying so")
    bad = _validate({"kind": "nope", "version": 0, "structures": [{"name": "x"}],
                     "industry": {"nonsense": {}}})
    check(len(bad) >= 4, f"all four problems come back together, not the first (got {len(bad)}: {bad})")
    check(any("location_id" in p for p in bad), "a structure with no location is named")
    check(any("nonsense" in p for p in bad), "an unknown industry section is named")


def test_export_shape() -> None:
    from app.config_io import export_config, CONFIG_KIND
    print("\nthe export carries choices, not registries, and no unresolvable id:")
    doc = export_config(CTX_A)
    check(doc["kind"] == CONFIG_KIND and doc["version"] >= 1, "it is stamped with kind and version")
    check(len(doc["structures"]) == 2, f"both structures travel (got {len(doc['structures'])})")
    check("categories" not in doc["industry"]["reactions"],
          "the category registry does NOT travel — the importing deploy already has it")
    check(doc["industry"]["pins"] == {"capital_ship": LOC_2},
          f"a pin travels as the structure's location, not its row id (got {doc['industry']['pins']})")
    check(json.loads(json.dumps(doc)) == doc, "the whole document is JSON-serialisable as it stands")
    sotiyo = [s for s in doc["structures"] if s["location_id"] == LOC_2][0]
    check(sotiyo["me_rig_groups"] == ["capital_ship"],
          f"rig families travel as a list (got {sotiyo['me_rig_groups']!r})")


def test_bad_import_writes_nothing() -> None:
    from fastapi import HTTPException
    from app.config_io import ConfigImport, import_config, export_config
    print("\na document with a problem in it changes nothing:")
    before = _state(CTX_B)
    doc = export_config(CTX_A)
    doc["structures"].append({"name": "no location here"})
    try:
        import_config(CTX_B, ConfigImport(config=doc))
        check(False, "a bad document is refused")
    except HTTPException as e:
        check(e.status_code == 400, f"refused with 400 (got {e.status_code})")
        check(bool(e.detail.get("problems")), "and says what is wrong with it")
    except Exception as e:
        # Caught rather than allowed to abort the run: a validator that stops early lets the bad
        # document reach the writers, and what comes back is whatever they happen to raise. That is
        # a failure of this check, and the "unchanged" assertion below is the half worth reaching.
        check(False, f"refused cleanly rather than crashing in the writers ({type(e).__name__}: {e})")
    check(_state(CTX_B) == before, "the importing account is byte-for-byte unchanged")


def test_dry_run_writes_nothing() -> None:
    from app.config_io import ConfigImport, import_config, export_config
    print("\na dry run reports what would change and writes nothing:")
    before = _state(CTX_B)
    doc = export_config(CTX_A)
    out = import_config(CTX_B, ConfigImport(config=doc, dry_run=True))
    check(out["dry_run"] is True, "it says it was a dry run")
    check(out["structures"]["added"] == 2, f"it counts the structures it would add ({out['structures']})")
    check(out["placeholders"]["added"] == 1, f"...and the placeholder ({out['placeholders']})")
    check(_state(CTX_B) == before, "nothing was written")


def test_round_trip() -> None:
    from app.config_io import ConfigImport, import_config, export_config
    from app.industry.build_setup import _available
    print("\nA's file, imported into B, reproduces A's configuration:")
    doc = export_config(CTX_A)
    out = import_config(CTX_B, ConfigImport(config=doc))
    avail = _available(CTX_B)
    check(all(avail.get(s) for s in ("reactions", "components", "job_length", "pins")),
          f"the gated sections really are on for this run (got {avail})")

    b = export_config(CTX_B)
    check(len(b["structures"]) == 2, f"both structures arrived (got {len(b['structures'])})")
    b_sotiyo = [s for s in b["structures"] if s["location_id"] == LOC_2][0]
    check(b_sotiyo["me_rig_groups"] == ["capital_ship"],
          f"rig families survived the trip as a list (got {b_sotiyo['me_rig_groups']!r})")
    check(b_sotiyo["name"] == "Test Sotiyo", "and the building is still the same building")

    check(b["industry"]["margin"]["margin_pct"] == 12.5, "the margin came across")
    check(b["industry"]["facility"]["facility_id"] == "sotiyo", "the facility preset came across")

    # THE id test. B's rows were minted by B's own sequence, so a pin written through verbatim
    # would name one of A's ids — which either does not exist here or is somebody else's building.
    if avail.get("pins"):
        a_rows, b_rows = _rows(CTX_A), _rows(CTX_B)
        check(a_rows[LOC_2] != b_rows[LOC_2],
              f"the two accounts really did mint different row ids ({a_rows[LOC_2]} vs {b_rows[LOC_2]})")
        check(_pins(CTX_B) == {"capital_ship": f"s:{b_rows[LOC_2]}"},
              f"the pin was re-resolved to B's own row (got {_pins(CTX_B)})")
        check(b["industry"]["pins"] == {"capital_ship": LOC_2},
              "...and B's own export names the same building A's did")
    else:
        check(any("pin" in s for s in out["skipped"]),
              f"pins are off for this account and the import SAYS so (skipped={out['skipped']})")

    check(len([p for p in b["placeholders"] if p["name"] == "Config Test Alt"]) == 1,
          "the placeholder character arrived")
    ph = [p for p in b["placeholders"] if p["name"] == "Config Test Alt"][0]
    check((ph["mfg_slots"], ph["rx_slots"], ph["max_planets"]) == (7, 5, 4),
          f"...with the capacity it declared (got {ph})")

    check(out["skipped"] == [], f"with everything enabled nothing is skipped (got {out['skipped']})")


def test_gated_section_is_reported() -> None:
    """A flag being off must cost that section and SAY so — not cost the whole import, and not pass
    in silence. `apply_patch` 403s a denied section, which for an import would mean one flag off
    throws away every other section in the file; dropping it quietly is how a user comes to believe
    a setting took effect when it did not."""
    from app.config_io import ConfigImport, import_config, export_config
    print("\na section whose flag is off is skipped BY NAME, and costs nothing else:")
    doc = export_config(CTX_A)
    was = _feature_states(["industry_job_length_policy"])
    _set_features({"industry_job_length_policy": "hidden"})
    try:
        out = import_config(CTX_B, ConfigImport(config=doc))
    finally:
        _set_features(was)
    check(any("job length" in s for s in out["skipped"]),
          f"the disabled section is named in skipped (got {out['skipped']})")
    check("margin" in out["sections"],
          f"...and the rest of the file was still written (got {out['sections']})")


def test_endpoints_are_gated() -> None:
    """Both endpoints are behind the rollout flag, not just the nav item that opens them.

    A gate that only hides a button is not a gate (see the Planet DB incident in `features.py`): the
    export returns the account's structures and character names, and a pasted URL reaches it exactly
    as the button does."""
    from fastapi import HTTPException
    from app.config_io import api_export_config, api_import_config, ConfigImport, FEATURE_KEY, CONFIG_KIND
    print("\nthe endpoints themselves are gated, not only the button:")
    was = _feature_states([FEATURE_KEY])
    _set_features({FEATURE_KEY: "hidden"})
    try:
        for name, call in (("export", lambda: api_export_config(CTX_A)),
                           ("import", lambda: api_import_config(
                               ConfigImport(config={"kind": CONFIG_KIND, "version": 1}), CTX_A))):
            try:
                call()
                check(False, f"{name} is refused when the feature is off")
            except HTTPException as e:
                check(e.status_code == 403, f"{name} 403s when the feature is off (got {e.status_code})")
        _set_features({FEATURE_KEY: "public"})
        check(isinstance(api_export_config(CTX_A).get("config"), dict),
              "...and answers once it is on")
    finally:
        _set_features(was)


def test_replace_spares_region_markets() -> None:
    """`pp_markets` holds two different things, and "replace structures" must only mean one.

    A followed REGION market is the account's pricing chain. It is not in the export (the file is
    structures), so a merge that considered every row would find it unmentioned and delete it — a
    self-backup, restored with the box ticked, silently emptying the pricing chain and falling back
    to group defaults or Jita. Found in review before it shipped; pinned here because the two kinds
    share a table and nothing else stops them sharing a fate."""
    from app.config_io import ConfigImport, import_config, export_config
    print("\n\"remove structures this file does not mention\" means STRUCTURES:")
    _add_market(CTX_B, "region", LOC_REGION, "The Forge (test)")
    doc = export_config(CTX_A)
    check(not any(s["location_id"] == LOC_REGION for s in doc["structures"]),
          "a followed region is not in the export in the first place")
    out = import_config(CTX_B, ConfigImport(config=doc, replace_structures=True))
    rows = _market_rows(CTX_B)
    check(any(r["kind"] == "region" and r["location_id"] == LOC_REGION for r in rows),
          f"the followed region survived a replace (rows now: {rows})")
    check(out["structures"]["removed"] == 0,
          f"...and was not counted as a structure removed ({out['structures']})")


def test_inactive_row_is_not_shadowed() -> None:
    """A deactivated structure at a location the file names is that building, switched off.

    Matching it and leaving it inactive reported "updated" for a row still invisible to every
    reader; inserting beside it would be the duplicate the merge exists to prevent. So it is matched
    AND switched back on — importing a structure is saying you build there."""
    from app.config_io import ConfigImport, import_config, export_config
    print("\nan inactive row at the same location is revived, not shadowed:")
    _wipe(CTX_B)
    _add_market(CTX_B, "structure", LOC_1, "Test Raitaru (off)", active=0)
    out = import_config(CTX_B, ConfigImport(config=export_config(CTX_A)))
    rows = [r for r in _market_rows(CTX_B) if r["location_id"] == LOC_1]
    check(len(rows) == 1, f"still one row for that building, not two (got {rows})")
    check(rows and rows[0]["active"] == 1, f"...and it is active again (got {rows})")
    check(len(export_config(CTX_B)["structures"]) == 2,
          "both structures are visible to the reader afterwards")
    check(out["structures"]["added"] == 1 and out["structures"]["updated"] == 1,
          f"reported as one added, one updated ({out['structures']})")


def test_manual_structures_gated() -> None:
    """A hand-described structure is an unverified claim about a building, and `add_manual_structure`
    gates it and forces `price_from=0` so a made-up building can never price a market. A file is an
    easier route into the same table than that endpoint, so it obeys the same two rules."""
    from app.config_io import ConfigImport, import_config, export_config
    print("\na hand-described structure obeys the same rules a file cannot get round:")
    _wipe(CTX_B)
    doc = export_config(CTX_A)
    doc["structures"] = [{"kind": "structure", "location_id": LOC_MANUAL, "name": "Made Up Ltd",
                          "price_from": 1, "build_mfg": 1}]
    doc["industry"]["pins"] = {}
    was = _feature_states(["industry_manual_structures"])
    _set_features({"industry_manual_structures": "hidden"})
    try:
        out = import_config(CTX_B, ConfigImport(config=doc))
        check(out["structures"]["added"] == 0, f"with the flag off it is not created ({out['structures']})")
        check(any("hand-described" in s for s in out["skipped"]),
              f"...and the import says why (skipped={out['skipped']})")
        _set_features({"industry_manual_structures": "public"})
        out = import_config(CTX_B, ConfigImport(config=doc))
        check(out["structures"]["added"] == 1, f"with the flag on it is created ({out['structures']})")
        got = [s for s in export_config(CTX_B)["structures"] if s["location_id"] == LOC_MANUAL]
        check(got and not got[0].get("price_from"),
              f"...but never as a pricing source (got price_from={got and got[0].get('price_from')})")
    finally:
        _set_features(was)
        _wipe(CTX_B)


def test_writers_cannot_be_reached_by_a_bad_value() -> None:
    """Everything a writer cannot survive is refused UP FRONT, because the writes are not one
    transaction: structures commit before the industry patch, the reaction settings and the rest.

    Each case below reached a writer during review and produced a 500 or a late 400 with the
    structures already merged — a half-applied import, which is worse than a refused one because
    nothing tells the user which half took."""
    from fastapi import HTTPException
    from app.config_io import ConfigImport, import_config, export_config, _validate
    print("\na value a later writer would choke on is refused before the first write:")
    base = export_config(CTX_A)

    cases = [
        ("a nested object where a setting goes",
         lambda d: d["industry"].__setitem__("facility", {"facility_id": {"nested": True}})),
        ("an unrecognised solar system",
         lambda d: d.__setitem__("reaction_settings", {
             "import_isk_per_m3": 1200, "export_isk_per_m3": 1200, "export_collateral_pct": 0.005,
             "reaction_system": "Nowhere At All XYZ"})),
        ("a structure with no name",
         lambda d: d["structures"].append({"kind": "structure", "location_id": 9000000123})),
        ("the same location twice",
         lambda d: d["structures"].append(dict(d["structures"][0]))),
        ("an unknown structure kind",
         lambda d: d["structures"].append({"kind": "wormhole", "location_id": 9000000124,
                                           "name": "Nope"})),
    ]
    for label, mutate in cases:
        doc = copy.deepcopy(base)
        mutate(doc)
        check(bool(_validate(doc)), f"{label} is a validation problem")
        before = _state(CTX_B)
        try:
            import_config(CTX_B, ConfigImport(config=doc))
            check(False, f"{label} is refused")
        except HTTPException as e:
            check(e.status_code == 400, f"{label} → 400 (got {e.status_code})")
        except Exception as e:
            check(False, f"{label} reached a writer ({type(e).__name__}: {e})")
        check(_state(CTX_B) == before, f"...and nothing was written for it")
        # The dry run the UI shows must reach the SAME verdict, or a preview says "fine" and the
        # apply behind it fails halfway.
        try:
            import_config(CTX_B, ConfigImport(config=doc, dry_run=True))
            check(False, f"{label} is refused by the preview too")
        except HTTPException:
            check(True, f"{label} is refused by the preview too")
        except Exception as e:
            check(False, f"{label} crashed the preview ({type(e).__name__}: {e})")


def test_import_twice() -> None:
    from app.config_io import ConfigImport, import_config, export_config
    print("\nimporting the same file twice is the same statement made twice, not a doubled roster:")
    doc = export_config(CTX_A)
    out = import_config(CTX_B, ConfigImport(config=doc))
    check(out["structures"]["added"] == 0 and out["structures"]["updated"] == 2,
          f"the structures were updated in place ({out['structures']})")
    check(out["placeholders"]["added"] == 0 and out["placeholders"]["updated"] == 1,
          f"the placeholder was matched by name ({out['placeholders']})")
    b = export_config(CTX_B)
    check(len(b["structures"]) == 2, f"still two structures (got {len(b['structures'])})")
    check(len([p for p in b["placeholders"] if p["name"] == "Config Test Alt"]) == 1,
          "still one placeholder of that name")


def test_replace_structures() -> None:
    from app.config_io import ConfigImport, import_config, export_config
    print("\nreplace_structures is opt-in, and the default never deletes:")
    doc = export_config(CTX_A)
    trimmed = copy.deepcopy(doc)
    trimmed["structures"] = [s for s in trimmed["structures"] if s["location_id"] == LOC_1]
    trimmed["industry"]["pins"] = {}

    out = import_config(CTX_B, ConfigImport(config=trimmed))
    check(out["structures"]["removed"] == 0, "a merge removes nothing the file does not mention")
    check(len(export_config(CTX_B)["structures"]) == 2, "so B still has both")

    out = import_config(CTX_B, ConfigImport(config=trimmed, replace_structures=True))
    check(out["structures"]["removed"] == 1, f"asked to replace, it removes the extra ({out['structures']})")
    check(len(export_config(CTX_B)["structures"]) == 1, "B now matches the file")


def test_unknown_sources_dropped() -> None:
    from app.config_io import ConfigImport, import_config, export_config
    print("\na stock source this account has never scanned is dropped and counted, never written:")
    doc = export_config(CTX_A)
    doc["sources"] = {"enabled_keys": ["container:999999999", "corp:999999999"], "sets": []}
    out = import_config(CTX_B, ConfigImport(config=doc, dry_run=True))
    check(out["sources"]["unknown"] == 2 and out["sources"]["enabled"] == 0,
          f"both unknown keys are reported, none enabled (got {out['sources']})")


def main() -> int:
    for ctx in (CTX_A, CTX_B):
        _wipe(ctx)
    restore = _feature_states(GATED)
    _set_features({k: "public" for k in GATED})
    _seed(CTX_A)
    try:
        test_validation()
        test_export_shape()
        test_bad_import_writes_nothing()
        test_dry_run_writes_nothing()
        test_round_trip()
        test_gated_section_is_reported()
        test_endpoints_are_gated()
        test_writers_cannot_be_reached_by_a_bad_value()
        test_import_twice()
        test_replace_structures()
        test_unknown_sources_dropped()
        test_replace_spares_region_markets()
        test_inactive_row_is_not_shadowed()
        test_manual_structures_gated()
    finally:
        _set_features(restore)
        for ctx in (CTX_A, CTX_B):
            _wipe(ctx)
    print("\n" + ("FAILED: " + "; ".join(_fails) if _fails else "all checks passed"))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
