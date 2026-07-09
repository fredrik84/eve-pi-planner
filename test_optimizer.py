"""
Correctness tests for app/optimizer.py's LP solve.

Added when the solver was swapped from scipy.optimize.linprog(method="highs") to calling
highspy (the HiGHS binding) directly, to drop the scipy/numpy dependency chain from the image.
Both delegate to the same underlying HiGHS solver, but the swap rewrote the call site (variable
bounds, objective, constraints, status handling) by hand — these tests exist to prove that
rewrite is numerically correct, not just "doesn't crash."

Two layers:
  1. In-process tests against a small synthetic pi_data (no SDE/DB needed) — hand-computable
     optimal answers, so they keep working even if the real SDE data changes.
  2. A live smoke test against a running container's /api/optimize, to catch anything that only
     breaks in the real image (e.g. highspy failing to import/install there).

Usage:
    python test_optimizer.py [--url http://localhost:8000]
"""

import argparse
import json
import sys
import urllib.request

sys.path.insert(0, ".")
from app.optimizer import optimize_production  # noqa: E402


def check(cond: bool, msg: str) -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    return cond


# ── Synthetic pi_data: three P0s (Ore, Gas, Iron), four P2 outputs ──────────────────────────
ORE, GAS, IRON = 900001, 900002, 900003
ALPHA, BETA, ZETA, RARE, WIDGET = 900010, 900011, 900012, 900099, 900013

PI_DATA = {
    "types": {
        ORE: {"name": "Test Ore"},
        GAS: {"name": "Test Gas"},
        IRON: {"name": "Test Iron"},
        ALPHA: {"name": "Test Alpha", "pi_tier": 2},
        BETA: {"name": "Test Beta", "pi_tier": 2},
        ZETA: {"name": "Test Zeta", "pi_tier": 2},
        RARE: {"name": "Rare Mineral"},
        WIDGET: {"name": "Test Widget", "pi_tier": 2},
    },
    "schematics": {
        ALPHA: {"inputs": [{"type_id": ORE, "quantity": 2}], "output_qty": 1},
        BETA: {"inputs": [{"type_id": ORE, "quantity": 4}], "output_qty": 1},
        ZETA: {"inputs": [{"type_id": RARE, "quantity": 1}], "output_qty": 1},
        # Iron-only, not shared with Alpha/Beta/Zeta -- keeps the single-resource test below
        # from landing on a degenerate LP tie (see comment there).
        WIDGET: {"inputs": [{"type_id": IRON, "quantity": 3}], "output_qty": 1},
    },
    "name_to_id": {
        "test ore": ORE, "test gas": GAS, "test iron": IRON, "test alpha": ALPHA,
        "test beta": BETA, "test zeta": ZETA, "rare mineral": RARE, "test widget": WIDGET,
    },
}


def test_uncapped_single_resource() -> bool:
    print(f"\n{'='*60}\n  Uncapped single-resource optimum\n{'='*60}")
    ok = True
    # Widget costs 3 Iron/unit and is the only thing Iron can make, no order ->
    # maximize floor(97/3) = 32, leftover 1. (Using Ore/Alpha here would be a degenerate LP:
    # Beta also converts Ore alone, and with no order at all every weight equals its own
    # resource coefficient, so an all-Beta and an all-Alpha allocation score identically and
    # the solver is free to pick either -- not a bug, just not a single-answer scenario.)
    result = optimize_production("Test Iron\t97", "", PI_DATA)
    plan = {p["name"]: p for p in result["plan"]}
    ok &= check("Test Widget" in plan, "Test Widget is producible")
    if "Test Widget" in plan:
        ok &= check(plan["Test Widget"]["quantity"] == 32, f"quantity=32 (got {plan['Test Widget']['quantity']})")
    ok &= check(result["leftover"].get("Test Iron") == 1, f"leftover Iron=1 (got {result['leftover'].get('Test Iron')})")
    return ok


def test_order_fully_covered() -> bool:
    print(f"\n{'='*60}\n  Order fully coverable\n{'='*60}")
    ok = True
    # Alpha ordered qty=10 costs 2 Ore/unit, huge inventory -> exactly 10, fill_pct=100
    result = optimize_production("Test Ore\t100000", "Test Alpha\t10", PI_DATA)
    plan = {p["name"]: p for p in result["plan"]}
    a = plan.get("Test Alpha")
    ok &= check(a is not None and a["quantity"] == 10, f"Alpha quantity=10 (got {a and a['quantity']})")
    ok &= check(a is not None and a["fill_pct"] == 100.0, f"Alpha fill_pct=100.0 (got {a and a['fill_pct']})")
    return ok


def test_order_partially_covered() -> bool:
    print(f"\n{'='*60}\n  Order partially coverable (resource-capped)\n{'='*60}")
    ok = True
    # Alpha ordered qty=50 costs 2 Ore/unit, only 40 Ore -> capped at 20, fill_pct=40
    result = optimize_production("Test Ore\t40", "Test Alpha\t50", PI_DATA)
    plan = {p["name"]: p for p in result["plan"]}
    a = plan.get("Test Alpha")
    ok &= check(a is not None and a["quantity"] == 20, f"Alpha quantity=20 (got {a and a['quantity']})")
    ok &= check(a is not None and a["fill_pct"] == 40.0, f"Alpha fill_pct=40.0 (got {a and a['fill_pct']})")
    ok &= check(result["leftover"].get("Test Ore") is None, "no Ore leftover (fully consumed)")
    return ok


def test_two_outputs_share_resource() -> bool:
    print(f"\n{'='*60}\n  Ordered + non-ordered outputs contend for one resource\n{'='*60}")
    ok = True
    # Ore=100. Alpha ordered qty=10 (2 Ore/unit) capped -> consumes 20.
    # Remaining 80 Ore all goes to uncapped Beta (4 Ore/unit) -> floor(80/4)=20.
    # Total consumption = 20*2 + 20*4 = 100 -> exact utilization, zero leftover.
    result = optimize_production("Test Ore\t100", "Test Alpha\t10", PI_DATA)
    plan = {p["name"]: p for p in result["plan"]}
    a, b = plan.get("Test Alpha"), plan.get("Test Beta")
    ok &= check(a is not None and a["quantity"] == 10, f"Alpha quantity=10 (got {a and a['quantity']})")
    ok &= check(b is not None and b["quantity"] == 20, f"Beta quantity=20 (got {b and b['quantity']})")
    ok &= check(result["leftover"].get("Test Ore") is None, "no Ore leftover (fully consumed)")
    ok &= check(result["utilization"] == 100.0, f"utilization=100.0 (got {result['utilization']})")
    return ok


def test_not_producible_missing_input() -> bool:
    print(f"\n{'='*60}\n  Ordered item with a missing input\n{'='*60}")
    ok = True
    # Zeta needs Rare Mineral, which isn't in inventory at all.
    result = optimize_production("Test Ore\t100", "Test Zeta\t5", PI_DATA)
    ok &= check(len(result["not_producible"]) == 1, f"1 not_producible entry (got {len(result['not_producible'])})")
    if result["not_producible"]:
        entry = result["not_producible"][0]
        ok &= check(entry["name"] == "Test Zeta", f"name=Test Zeta (got {entry['name']})")
        ok &= check(entry["missing"] == ["Rare Mineral"], f"missing=[Rare Mineral] (got {entry['missing']})")
    return ok


def test_empty_inventory() -> bool:
    print(f"\n{'='*60}\n  Empty inventory short-circuits before the solver\n{'='*60}")
    ok = True
    result = optimize_production("", "Test Alpha\t5", PI_DATA)
    ok &= check(result["plan"] == [], "empty plan")
    ok &= check(result["utilization"] == 0.0, "utilization=0.0")
    return ok


def test_live_smoke(base: str) -> bool:
    print(f"\n{'='*60}\n  POST /api/optimize against {base} (real SDE data)\n{'='*60}")
    ok = True
    body = json.dumps({"inventory": "Water\t50000\nBase Metals\t9000", "order": ""}).encode()
    req = urllib.request.Request(
        f"{base}/api/optimize", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status, data = resp.status, json.loads(resp.read())
    except Exception as e:
        return check(False, f"request succeeded (got exception: {e})")

    ok &= check(status == 200, f"HTTP 200 (got {status})")
    ok &= check("error" not in data, f"no solver error (got {data.get('error')})")
    ok &= check(isinstance(data.get("plan"), list) and len(data["plan"]) > 0, "non-empty plan")

    # Internal consistency: recompute consumption from the returned per-unit inputs and confirm
    # it never exceeds the supplied inventory (proves the solver's constraints actually held).
    supplied = {"Water": 50000, "Base Metals": 9000}
    consumed: dict[str, float] = {}
    for item in data.get("plan", []):
        for inp in item["inputs"]:
            consumed[inp["name"]] = consumed.get(inp["name"], 0.0) + inp["per_unit"] * item["quantity"]
    for name, used in consumed.items():
        if name in supplied:
            ok &= check(used <= supplied[name] + 1e-6, f"{name} consumption {used} <= supplied {supplied[name]}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    args = parser.parse_args()
    base = args.url.rstrip("/")

    results = [
        test_uncapped_single_resource(),
        test_order_fully_covered(),
        test_order_partially_covered(),
        test_two_outputs_share_resource(),
        test_not_producible_missing_input(),
        test_empty_inventory(),
        test_live_smoke(base),
    ]

    print(f"\n{'='*60}")
    if all(results):
        print("  ALL TESTS PASSED")
        return 0
    print("  SOME TESTS FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
