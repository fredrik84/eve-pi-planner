"""
Correctness tests for the fixed-unit "customer order" mode in Reactions (app/reactions.py) —
create a target-quantity order, get the runs/materials/cost/time report, commit runs to real
reaction slots, and the status/delete lifecycle. Hit as live requests against a running
container via a fabricated pp_sessions cookie (same pattern as test_colony_alerts.py's live
smoke test) — this feature is mostly wiring on top of existing chain-math helpers
(_explode_shopping_list, _explode_chain_tiers, _character_capacities), so the useful thing to
verify is the wiring and the arithmetic, not re-deriving the chain math itself.

Needs at least one group's moon-goo price sheet populated (pp_moon_goo_prices) and outbound
network access for live market prices, same as the rest of the Reactions feature — if neither
is available, test_order_lifecycle degrades to a single skip-with-explanation rather than a
hard failure.

Usage:
    python test_reactions.py [--url http://localhost:8000]
"""
import argparse
import json
import math
import secrets
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, ".")
from app.sde import get_connection  # noqa: E402

FAKE_CTX = 777051
FAKE_CID = 990051


def check(cond: bool, msg: str) -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    return cond


def _cleanup():
    con = get_connection()
    con.execute("DELETE FROM pp_reaction_assignments WHERE character_id=?", (FAKE_CID,))
    con.execute("DELETE FROM pp_reaction_orders WHERE context_id=?", (FAKE_CTX,))
    con.execute("DELETE FROM pp_sessions WHERE context_id=?", (FAKE_CTX,))
    con.execute("DELETE FROM pp_characters WHERE character_id=?", (FAKE_CID,))
    con.commit()
    con.close()


def _seed_session() -> str:
    """A tracked character with real reaction slots (5 levels of Mass Reactions -> 6 slots) and
    no cached industry jobs — so _character_capacities reports every slot free, letting the
    assign tests actually commit real pp_reaction_assignments rows."""
    token = secrets.token_urlsafe(24)
    con = get_connection()
    con.execute(
        "INSERT INTO pp_characters (character_id, character_name, context_id, scopes, "
        "mass_reactions, advanced_mass_reactions) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT (character_id) DO UPDATE SET context_id=excluded.context_id, "
        "scopes=excluded.scopes, mass_reactions=excluded.mass_reactions",
        (FAKE_CID, "Test Reactor", FAKE_CTX, "read_character_jobs", 5, 0),
    )
    con.execute(
        "INSERT INTO pp_sessions (token, character_id, context_id, created_at) VALUES (?,?,?,?)",
        (token, FAKE_CID, FAKE_CTX, datetime.now(timezone.utc).isoformat()),
    )
    con.commit()
    con.close()
    return token


class Api:
    def __init__(self, base: str, token: str):
        self.base = base
        self.token = token

    def _req(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.base}{path}", data=data, method=method)
        req.add_header("Cookie", f"pp_session={self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def get(self, path):
        return self._req("GET", path)

    def post(self, path, body=None):
        return self._req("POST", path, body if body is not None else {})

    def delete(self, path):
        return self._req("DELETE", path)


def _find_test_product(api: Api):
    """A real reachable single-tier reaction product (no chain tiers) to test against — not
    hardcoded, since which products are reachable depends on the SDE build and which group has
    priced moon goo. Picks the first single-tier one from the live opportunity list."""
    status, data = api.get("/api/reactions/opportunities")
    if status != 200:
        return None
    for o in data.get("opportunities", []):
        if o["steps"] == 1 and not o.get("chain_tiers") and o["top_level_runs"] > 0:
            return o
    return None


def test_order_lifecycle(api: Api) -> bool:
    print(f"\n{'='*60}\n  Customer order: create -> report -> assign -> status -> delete-guard\n{'='*60}")
    ok = True
    product = _find_test_product(api)
    if not check(product is not None, "found a reachable single-tier product to test against"):
        print("  (no priced moon goo sheet or no live market reachability right now — skipping the rest)")
        return ok

    # opportunities' own "output_qty" field is the MAX ACHIEVABLE total, not the formula's
    # per-run yield (see app.reactions._build_opportunities) — derive the real per-run yield
    # from it and top_level_runs, then pick a deliberately non-round target so ceil() rounding
    # actually gets exercised (a target that's exactly N runs' worth wouldn't catch an off-by-one).
    per_run_yield = product["output_qty"] / product["top_level_runs"]
    target_qty = per_run_yield * 2 + 1
    expected_runs = math.ceil(target_qty / per_run_yield)

    status, data = api.post("/api/reactions/orders", {
        "type_id": product["type_id"], "target_qty": target_qty, "client_name": "Test Client",
    })
    if not check(status == 200, f"create order succeeds (got {status}: {data})"):
        return ok
    order = data["order"]
    order_id = order["id"]
    ok &= check(order["top_level_runs"] == expected_runs,
                f"top_level_runs = ceil(target/per-run yield) (expected {expected_runs}, got {order['top_level_runs']})")
    ok &= check(order["assigned_runs"] == 0, "starts with nothing assigned")
    ok &= check(order["status"] == "open", "starts open")
    ok &= check(len(data["materials"]) > 0, "materials report is non-empty")
    ok &= check(data["cost"]["total_cost"] > 0, "cost report is non-empty")
    ok &= check(abs((data["cost"]["material_cost"] + data["cost"]["job_cost"]) - data["cost"]["total_cost"]) < 0.01,
                "total_cost = material_cost + job_cost")

    status, listed = api.get("/api/reactions/orders")
    ok &= check(status == 200 and any(o["id"] == order_id for o in listed["orders"]),
                "new order appears in the list endpoint")

    # Assign a single run — must occupy a real reaction slot (see _allocate_and_insert) and show
    # up, order-tagged, on GET /api/reactions/jobs (the same endpoint the dashboard reads).
    status, before_jobs = api.get("/api/reactions/jobs")
    free_before = before_jobs["free_slots"]

    status, assign_data = api.post(f"/api/reactions/orders/{order_id}/assign", {"runs": 1})
    ok &= check(status == 200, f"assign 1 run succeeds (got {status}: {assign_data})")
    if status == 200:
        ok &= check(assign_data["runs_assigned"] == 1, "assigned exactly the 1 requested run")
        ok &= check(assign_data["order"]["assigned_runs"] == 1, "order.assigned_runs incremented to 1")
        ok &= check(len(assign_data["characters"]) == 1, "landed on exactly one character")

    status, after_jobs = api.get("/api/reactions/jobs")
    free_after = after_jobs["free_slots"]
    ok &= check(free_after == free_before - 1, f"a real reaction slot got occupied (free {free_before} -> {free_after})")
    pending_match = None
    for c in after_jobs.get("characters", []):
        for p in c.get("pending", []):
            if p.get("order_id") == order_id:
                pending_match = p
    ok &= check(pending_match is not None, "the pending assignment is tagged with this order's id")
    if pending_match:
        ok &= check(pending_match.get("order_label") == "Test Client",
                     f"pending assignment carries the client name as its order label (got {pending_match.get('order_label')})")

    # Can't delete once something's been committed — must cancel instead.
    status, del_data = api.delete(f"/api/reactions/orders/{order_id}")
    ok &= check(status == 400, f"delete is blocked once assigned_runs > 0 (got {status}: {del_data})")

    # Assign everything remaining (runs=None), then confirm a further assign is rejected.
    status, assign_rest = api.post(f"/api/reactions/orders/{order_id}/assign", {})
    ok &= check(status == 200 and assign_rest["order"]["assigned_runs"] == order["top_level_runs"],
                f"assigning the rest brings assigned_runs to top_level_runs (got {assign_rest.get('order', {}).get('assigned_runs')})")
    status, over_data = api.post(f"/api/reactions/orders/{order_id}/assign", {})
    ok &= check(status == 400, f"assigning again once fully committed is rejected (got {status}: {over_data})")

    # Manual completion — this tool has no way to know a real job finished, so it must never
    # auto-flip status just because assigned_runs caught up to top_level_runs.
    status, status_data = api.post(f"/api/reactions/orders/{order_id}/status", {"status": "completed"})
    ok &= check(status == 200 and status_data["order"]["status"] == "completed", "manual 'mark completed' works")

    # A fresh, never-assigned order CAN be deleted outright.
    status, order2_data = api.post("/api/reactions/orders", {
        "type_id": product["type_id"], "target_qty": per_run_yield,
    })
    ok &= check(status == 200, "second (throwaway) order creates fine")
    if status == 200:
        order2_id = order2_data["order"]["id"]
        status, del2 = api.delete(f"/api/reactions/orders/{order2_id}")
        ok &= check(status == 200, f"delete succeeds when nothing's been assigned yet (got {status}: {del2})")

    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    args = parser.parse_args()
    base = args.url.rstrip("/")

    _cleanup()
    token = _seed_session()
    api = Api(base, token)
    results = [test_order_lifecycle(api)]
    _cleanup()

    print(f"\n{'='*60}")
    if all(results):
        print("  ALL TESTS PASSED")
        return 0
    print("  SOME TESTS FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
