"""
Distribution test suite for the PI planner.

Usage:
    python test_distribution.py [--url https://eve-pi.failed.name]

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

    # ── Overproduction priority check ───────────────────────────────────────
    # Extra slots (beyond baseline) should prefer:
    #   1. rel=2 types (they run out first)
    #   2. lowest-yielding planets (improve the weakest link)
    # We check: no rel=1 type got more slots than any rel=2 type at same ext_slots.
    rel2 = [d for d in distribution if d["rel"] == 2]
    rel1 = [d for d in distribution if d["rel"] == 1]
    if rel2 and rel1:
        min_rel2 = min(d["actual"] for d in rel2)
        max_rel1 = max(d["actual"] for d in rel1)
        if max_rel1 > min_rel2:
            print(f"\n  OVERPROD WARNING: a rel=1 type ({max_rel1}) got more slots than "
                  f"a rel=2 type ({min_rel2})")
            overall_pass = False
        else:
            print(f"\n  Overproduction priority OK  (min rel=2: {min_rel2}, max rel=1: {max_rel1})")

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
    parser.add_argument("--url", default="https://eve-pi.failed.name")
    args = parser.parse_args()
    base = args.url.rstrip("/")

    results = []
    for case in CASES:
        ok = run_case(base, case)
        results.append((case["name"], ok))

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
