"""
Distribution test suite for the PI planner.

Usage:
    python test_distribution.py [--url https://eve-pi-dev.failed.name]

Requires DEBUG_PI=true and DEBUG_CONTEXT_ID set in the server's .env.
"""

import argparse
import json
import sys
import urllib.request
import urllib.error

SHPC_TYPE_ID = 2872       # Self-Harmonizing Power Core (P4)
ROBOTICS_TYPE_ID = 9848   # fuel-block component, base qty 1 (no ME benefit)

CASES = [
    {
        "name": "2 systems, use_existing=True",
        "body": {
            "type_id":        SHPC_TYPE_ID,
            "factories":      15,
            "chosen_systems": ["01B-88", "5-U12M"],
            "use_existing":   True,
        },
    },
    {
        "name": "2 systems, use_existing=False",
        "body": {
            "type_id":        SHPC_TYPE_ID,
            "factories":      15,
            "chosen_systems": ["01B-88", "5-U12M"],
            "use_existing":   False,
        },
    },
    {
        "name": "1 system (DE-IHK), use_existing=True",
        "body": {
            "type_id":        SHPC_TYPE_ID,
            "factories":      15,
            "chosen_systems": ["DE-IHK"],
            "use_existing":   True,
        },
    },
    {
        "name": "1 system (DE-IHK), use_existing=False",
        "body": {
            "type_id":        SHPC_TYPE_ID,
            "factories":      15,
            "chosen_systems": ["DE-IHK"],
            "use_existing":   False,
        },
    },
    {
        "name": "Single product, overproduction=0 (2 systems)",
        "body": {
            "type_id":            SHPC_TYPE_ID,
            "chosen_systems":     ["01B-88", "5-U12M"],
            "use_existing":       True,
            "overproduction_pct": 0,
        },
    },
    {
        "name": "Single product, overproduction=30 (2 systems)",
        "body": {
            "type_id":            SHPC_TYPE_ID,
            "chosen_systems":     ["01B-88", "5-U12M"],
            "use_existing":       True,
            "overproduction_pct": 30,
        },
    },
    {
        "name": "Fuel block basket (2 systems)",
        "body": {
            "fuelblock":      True,
            "chosen_systems": ["01B-88", "5-U12M"],
            "use_existing":   True,
        },
    },
    {
        "name": "Fuel block, import Robotics (2 systems)",
        "body": {
            "fuelblock":         True,
            "chosen_systems":    ["01B-88", "5-U12M"],
            "use_existing":      True,
            "import_components": [ROBOTICS_TYPE_ID],
        },
    },
    {
        "name": "Fuel block, T2 ME rig in null (2 systems)",
        "body": {
            "fuelblock":        True,
            "chosen_systems":   ["01B-88", "5-U12M"],
            "use_existing":     True,
            "rig_tier":         "t2",
            "rig_space":        "null",
            "structure_me_pct": 1.0,
        },
    },
]


def post(url: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()
        raise RuntimeError(f"HTTP {e.code}: {body_text}") from e


def run_case(base_url: str, case: dict) -> bool:
    name = case["name"]
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")

    try:
        result = post(f"{base_url}/api/debug/plan", case["body"])
    except RuntimeError as e:
        print(f"  ERROR: {e}")
        return False

    ext_slots    = result["ext_slots"]
    total_asgn   = result["total_assigned"]
    unassigned   = result["unassigned"]
    distribution = result["distribution"]
    out_of_sys   = result["out_of_system"]
    overall_pass = result["pass"]
    n_unassigned = sum(unassigned.values())
    if n_unassigned > 0:
        overall_pass = False

    print(f"  ext_slots={ext_slots}  assigned={total_asgn}  "
          f"unassigned={n_unassigned}")

    # ── Fuel-block basket checks ────────────────────────────────────────────
    fbpd = result.get("fuel_blocks_per_day")
    if fbpd is not None:
        lines = result.get("factory_lines") or []
        covered = sum(1 for l in lines if l["count"] > 0)
        print(f"  fuel_blocks_per_day={fbpd:,}  factory lines covered={covered}/{len(lines)}  "
              f"unplaced_fac={result.get('unplaced_factories')}")
        for l in lines:
            need = l.get("need_per_day", 0)
            slack = (l["units_per_day"] - need)
            print(f"    {l['name']:18s} planets={l['count']:2d}  units/day={l['units_per_day']:,.0f}  "
                  f"need/day={need:,.0f}  slack={slack:+,.0f}")
        if fbpd <= 0 or covered != len(lines):
            print("  FUEL BLOCK FAIL: zero output or a component line has no planet")
            overall_pass = False
        if result.get("unplaced_factories"):
            print(f"  FUEL BLOCK FAIL: {result['unplaced_factories']} factory planet(s) unplaced")
            overall_pass = False
        # Numbers must pan out: each line's production capacity must cover the manufacturing
        # demand at the reported block rate (allow 1% rounding slack on need).
        for l in lines:
            need = l.get("need_per_day", 0)
            if l["count"] > 0 and l["units_per_day"] + 1 < need * 0.99:
                print(f"  FUEL BLOCK FAIL: {l['name']} capacity {l['units_per_day']:.0f} "
                      f"< need {need:.0f}")
                overall_pass = False

    # ── Distribution table ──────────────────────────────────────────────────
    print(f"\n  {'P1 product':<28} {'rel':>4} {'expected':>9} {'actual':>7} {'delta':>6}  status")
    print(f"  {'-'*28} {'-'*4} {'-'*9} {'-'*7} {'-'*6}  ------")
    for d in distribution:
        marker = "OK" if d["ok"] else "FAIL"
        print(f"  {d['p1_name']:<28} {d['rel']:>4}  {d['expected']:>8.1f}  {d['actual']:>6}  "
              f"{d['delta']:>+6.1f}  {marker}")

    # ── Unassigned ──────────────────────────────────────────────────────────
    if unassigned:
        print(f"\n  Unassigned slots: {unassigned}")

    # ── Out-of-system assignments ───────────────────────────────────────────
    chosen = case["body"].get("chosen_systems", [])
    real_oos = [o for o in out_of_sys if not o["is_replace"]]
    if real_oos:
        print(f"\n  WARNING: {len(real_oos)} non-replace extractors outside chosen systems:")
        for o in real_oos:
            print(f"    {o['character']:30s}  {o['p0']:25s}  {o['system']} P{o['planet_num']}")

    # ── Production balance (density-aware) ──────────────────────────────────
    # Slots now track need ÷ density: a thin-deposit resource gets MORE extractors so its
    # production lands in the recipe ratio (minimising leftover P1). So the old "rel=2 always
    # ≥ rel=1" rule no longer holds — correctness is the per-P0 expected/actual deltas above
    # (already folded into result["pass"], where `expected` is the density-aware target).
    # Here we just surface production coverage = actual × density ÷ need: tight ⇒ no resource is
    # the volatile bottleneck. We DON'T fail on a wide spread — that's a planet-supply limit
    # (not enough rich planets for a resource), not a distribution bug.
    dens = result.get("density_est") or {}
    cov = [(d["p0_name"], d["actual"] * dens.get(d["p0_name"], 1.0) / d["rel"])
           for d in distribution if d["rel"]]
    if cov:
        lo = min(c for _, c in cov)
        hi = max(c for _, c in cov)
        spread = (hi - lo) / hi if hi else 0.0
        note = "planet-capped (a resource can't get enough rich planets)" if spread > 0.4 else "balanced"
        print(f"\n  Production coverage (actual × density ÷ need): {lo:.2f}–{hi:.2f}  "
              f"spread {spread * 100:.0f}%  ({note})")

    # ── Per-character summary ───────────────────────────────────────────────
    print(f"\n  Character assignments:")
    for c in result["characters"]:
        p0_summary = "  ".join(f"{p}x{n}" for p, n in sorted(c["by_p0"].items()))
        free_note = f"  FREE={c['free']}" if c["free"] else ""
        print(f"    {c['name']:30s}  {c['extractors']}/{c['max']} ext{free_note}")
        print(f"      {p0_summary}")

    status = "PASS" if overall_pass else "FAIL"
    print(f"\n  Result: {status}")
    return overall_pass


def get(url: str) -> dict:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()}") from e


def run_sysrec_bottleneck() -> bool:
    """In-process: a system's rank must respond to its WEAKEST input, not just its summed
    richness. Output is a min() over the recipe's inputs, so a system that is rich on average
    but has one starved resource runs the chain slower than a slightly poorer, balanced one.

    Uses a throwaway in-memory pp_planets, so it needs no live Planet DB. Run inside the
    container (needs the app's deps).
    """
    print(f"\n{'='*60}\n  System-rec bottleneck ranking (in-process)\n{'='*60}")
    try:
        import sqlite3
        from app.planner_recommendations import _system_recommendations_impl
    except Exception as e:
        print(f"  SKIP: not importable here ({e.__class__.__name__}) — run inside the container")
        return True

    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE pp_planets (system TEXT, constellation TEXT, planet_num INT,
                   planet_type TEXT, reactive_gas INT DEFAULT 0, noble_metals INT DEFAULT 0)""")
    # RICH: piles of Noble Metals, a single thin Reactive Gas planet (the binding input).
    # BALANCED: less Noble Metals overall, but Reactive Gas actually available.
    # Tuned so RICH clearly WINS the summed depth_score (104 vs 95) while losing badly on the
    # binding input (4.0 vs 40.0) — otherwise the test would pass without the bottleneck term.
    rows = [("RICH", "C", 1, "Gas", 12, 0)]
    rows += [("RICH", "C", i, "Barren", 0, 100) for i in range(2, 8)]
    rows += [("BALANCED", "C", i, "Gas", 55, 0) for i in range(1, 4)]
    rows += [("BALANCED", "C", i, "Barren", 0, 40) for i in range(4, 7)]
    con.executemany("INSERT INTO pp_planets VALUES (?,?,?,?,?,?)", rows)

    p0 = ["Reactive Gas", "Noble Metals"]
    recs = _system_recommendations_impl(p0, con, top_n=5, preferred_systems=1,
                                        p0_needs={"Reactive Gas": 1.0, "Noble Metals": 1.0})
    singles = [r for r in recs if len(r["systems_needed"]) == 1]
    for r in singles:
        print(f"  {r['systems_needed'][0]:<9} coverage={r['coverage']} "
              f"depth={r['depth_score']:.0f} bottleneck={r['bottleneck_density']} "
              f"({r['bottleneck_p0']})")

    ok = True
    if not singles:
        print("  FAIL: no single-system recommendations produced")
        return False
    rich = next((r for r in singles if r["systems_needed"] == ["RICH"]), None)
    bal = next((r for r in singles if r["systems_needed"] == ["BALANCED"]), None)
    if not rich or not bal:
        print("  FAIL: expected both test systems in the results")
        return False
    if rich["depth_score"] <= bal["depth_score"]:
        print("  FAIL: test data no longer exercises the case (RICH must win on summed depth)")
        ok = False
    if rich["bottleneck_p0"] != "Reactive Gas":
        print(f"  FAIL: RICH's bottleneck should be Reactive Gas, got {rich['bottleneck_p0']}")
        ok = False
    if singles[0]["systems_needed"] != ["BALANCED"]:
        print(f"  FAIL: ranked {singles[0]['systems_needed']} first — the summed score won over "
              "the starved input")
        ok = False

    print(f"\n  Result: {'PASS' if ok else 'FAIL'}")
    return ok


def run_fuelblock_invariants(base_url: str) -> bool:
    """Cross-request fuel-block sanity: material efficiency must raise the block rate
    (same PI makes more blocks), and importing a component must drop its factory line."""
    print(f"\n{'='*60}\n  Fuel-block invariants\n{'='*60}")
    ok = True
    common = {"fuelblock": True, "chosen_systems": ["01B-88", "5-U12M"], "use_existing": True}
    try:
        plain = post(f"{base_url}/api/debug/plan", common)
        with_me = post(f"{base_url}/api/debug/plan",
                       {**common, "rig_tier": "t2", "rig_space": "null", "structure_me_pct": 1.0})
        imported = post(f"{base_url}/api/debug/plan", {**common, "import_components": [ROBOTICS_TYPE_ID]})
    except RuntimeError as e:
        print(f"  ERROR: {e}")
        return False

    base_fbpd, me_fbpd = plain["fuel_blocks_per_day"], with_me["fuel_blocks_per_day"]
    print(f"  block rate: plain={base_fbpd:,}  with ME={me_fbpd:,}")
    if not (me_fbpd >= base_fbpd):
        print("  FAIL: material efficiency did not increase the block rate")
        ok = False

    imported_lines = [l["name"] for l in (imported.get("factory_lines") or [])]
    print(f"  import Robotics → factory lines: {imported_lines}  (fbpd={imported['fuel_blocks_per_day']:,})")
    if "Robotics" in imported_lines:
        print("  FAIL: imported component still has a factory line")
        ok = False
    if imported.get("unplaced_factories"):
        print("  FAIL: unplaced factories in import case")
        ok = False

    print(f"\n  Result: {'PASS' if ok else 'FAIL'}")
    return ok


def run_factory_budget_invariants() -> bool:
    """In-process: the factory budget must never claim more factories than a character can
    physically host. A char can only put one colony on a planet, so a factory-only char is
    capped at the factory system's allowed-planet count; in explicit mode (extractor_limit
    set) there's no one else to absorb the remainder, and it used to be dropped silently —
    leaving stats.factories (and products/day) describing colonies the plan never placed.

    Run this INSIDE the container (needs the app's deps); it does no DB or market I/O.
    """
    print(f"\n{'='*60}\n  Factory budget invariants (in-process)\n{'='*60}")
    try:
        from app.planner import _compute_slot_budget
    except Exception as e:
        print(f"  SKIP: not importable here ({e.__class__.__name__}) — run inside the container")
        return True

    ok = True
    # 3 extractor chars + 1 factory-only char, 6 planets each; P4 (0.5/hr), 9 P1 inputs.
    chars = [{"character_id": i, "effective_planets": 6, "extractor_limit": None} for i in (1, 2, 3)]
    chars.append({"character_id": 4, "effective_planets": 6, "extractor_limit": 0})
    p1_fracs = {i: (320.0 if i < 3 else 160.0) for i in range(9)}

    for cap, label in ((4, "4 B/T planets in system"), (8, "8 B/T planets in system")):
        ext, fac, shares, _auto, _p0fd, short = _compute_slot_budget(
            chars, 10, 0.5, 3600, 1, p1_fracs, per_char_fac_cap=cap)
        placeable = sum(shares.values())
        print(f"  cap={cap} ({label}): factories={fac} shares={shares} unplaceable={short}")
        if fac != placeable:
            print(f"  FAIL: budgeted {fac} factories but only {placeable} shares were handed out")
            ok = False
        if any(v > cap for v in shares.values()):
            print(f"  FAIL: a character got more factories than the {cap} planets it can colonise")
            ok = False
        if short < 0:
            print("  FAIL: negative unplaceable count")
            ok = False

    # The capped case must actually report the shortfall (regression: it was dropped silently).
    _e, _f, _s, _a, _p, short4 = _compute_slot_budget(
        chars, 10, 0.5, 3600, 1, p1_fracs, per_char_fac_cap=4)
    if short4 < 1:
        print("  FAIL: clipped factories were not reported as unplaceable")
        ok = False

    print(f"\n  Result: {'PASS' if ok else 'FAIL'}")
    return ok


def run_smoke_tests(base_url: str) -> bool:
    """Smoke-test the non-planner features: PI product list, Factory Layout generator
    (P1/P2/P4 templates), and the PI Planner (tab-1 inventory analyzer)."""
    print(f"\n{'='*60}\n  Feature smoke tests\n{'='*60}")
    ok = True

    # ── PI product list ─────────────────────────────────────────────────────
    try:
        products = get(f"{base_url}/api/pi-products").get("products", [])
        tiers = sorted({p.get("tier") for p in products})
        print(f"  /api/pi-products → {len(products)} products, tiers={tiers}")
        if len(products) < 60 or tiers != [1, 2, 3, 4]:
            print("  FAIL: unexpected PI product list")
            ok = False
        p1 = [p for p in products if p.get("tier") == 1]
    except RuntimeError as e:
        print(f"  /api/pi-products ERROR: {e}")
        return False

    # ── Factory Layout generator (P4 SHPC, a P2, a P1 extractor) ─────────────
    layout_cases = [
        ("P4 SHPC (Barren)", {"type_id": SHPC_TYPE_ID, "planet_type": "Barren", "launchpads": 3}),
        ("P2 Coolant (Barren)", {"type_id": 9832, "planet_type": "Barren", "launchpads": 3}),
    ]
    if p1:
        layout_cases.append(("P1 extractor", {"type_id": p1[0]["type_id"], "planet_type": "Barren"}))
    for name, body in layout_cases:
        try:
            d = post(f"{base_url}/api/layout", body)
            summary = d.get("summary", {})
            planets = d.get("planets", [])
            pph = summary.get("product_per_hour", 0)
            tmpl = planets[0].get("template", {}) if planets else {}
            np_, nl, nr = len(tmpl.get("P", [])), len(tmpl.get("L", [])), len(tmpl.get("R", []))
            print(f"  /api/layout {name:20s} pph={pph}  pins={np_} links={nl} routes={nr}")
            if not planets or pph <= 0 or np_ == 0:
                print(f"  FAIL: layout '{name}' returned no usable template")
                ok = False
        except RuntimeError as e:
            print(f"  /api/layout {name} ERROR: {e}")
            ok = False

    # ── PI Planner (tab-1 inventory analyzer) ────────────────────────────────
    inventory = "Water\t100000\nOxygen\t100000\nPlasmoids\t100000\nElectrolytes\t100000"
    try:
        d = post(f"{base_url}/api/analyze", {"inventory": inventory})
        results = d.get("results", {})
        producible = [i for items in results.values() for i in items if i.get("max_output", 0) > 0]
        print(f"  /api/analyze → tiers={list(results)}  producible items={len(producible)}")
        if not producible:
            print("  FAIL: analyzer produced nothing from a valid P1 inventory")
            ok = False
    except RuntimeError as e:
        print(f"  /api/analyze ERROR: {e}")
        ok = False

    print(f"\n  Result: {'PASS' if ok else 'FAIL'}")
    return ok


def main():
    parser = argparse.ArgumentParser()
    # Defaults to the LOCAL container, not production. These suites POST plans and read
    # debug endpoints; pointing them at prod by default meant a plain `python3 test_x.py`
    # ran against live users' service (and silently "passed" by testing prod, not your change).
    # Pass --url explicitly to aim at a deployed environment.
    parser.add_argument("--url", default="http://localhost:8000")
    args = parser.parse_args()
    base = args.url.rstrip("/")

    results = []
    for case in CASES:
        ok = run_case(base, case)
        results.append((case["name"], ok))

    results.append(("Factory budget invariants", run_factory_budget_invariants()))
    results.append(("System-rec bottleneck", run_sysrec_bottleneck()))
    results.append(("Fuel-block invariants", run_fuelblock_invariants(base)))
    results.append(("Feature smoke tests", run_smoke_tests(base)))

    print(f"\n{'='*60}")
    print("  SUMMARY")
    print(f"{'='*60}")
    all_pass = True
    for name, ok in results:
        status = "PASS" if ok else "FAIL"
        print(f"  {status}  {name}")
        if not ok:
            all_pass = False

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
