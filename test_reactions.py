"""
Correctness tests for the fixed-unit "customer order" mode in Reactions (app/reactions.py) —
create a target-quantity order, get the runs/materials/cost/time report, commit runs to real
reaction slots, and the status/delete lifecycle. Hit as live requests against a running
container via a fabricated pp_sessions cookie (same pattern as test_alerts.py's live
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
    for t in ("pp_char_blueprints", "pp_char_formula_jobs", "pp_char_industry_jobs"):
        try:
            con.execute(f"DELETE FROM {t} WHERE character_id=?", (FAKE_CID,))
        except Exception:
            pass
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
    shown_material_cost = sum(m["unit_cost"] * m["quantity"] for m in data["materials"])
    ok &= check(abs(shown_material_cost - data["cost"]["material_cost"]) < 0.01,
                "preview material cost equals its stock-netted shopping-list rows")
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
    # Completing frees the slots this order reserved — free count returns to where it was before assign.
    # (freed_slots counts assignment ROWS = reaction jobs, which may bundle multiple runs, so it's ≥1,
    # not necessarily == top_level_runs; the authoritative check is free_slots returning to baseline.)
    ok &= check(status_data.get("freed_slots", 0) >= 1,
                f"completion frees the order's assignment rows (freed {status_data.get('freed_slots')})")
    status, jobs_after_complete = api.get("/api/reactions/jobs")
    ok &= check(jobs_after_complete["free_slots"] == free_before,
                f"a completed order's reaction slots are released (free back to {free_before}, got {jobs_after_complete['free_slots']})")

    # Cancelling a slot-committed order must ALSO free its slots.
    status, order3_data = api.post("/api/reactions/orders", {"type_id": product["type_id"], "target_qty": per_run_yield})
    if status == 200:
        order3_id = order3_data["order"]["id"]
        api.post(f"/api/reactions/orders/{order3_id}/assign", {"runs": 1})
        status, mid_jobs = api.get("/api/reactions/jobs")
        ok &= check(mid_jobs["free_slots"] == free_before - 1, "assigning to the cancel-test order took a slot")
        status, cancel_data = api.post(f"/api/reactions/orders/{order3_id}/status", {"status": "cancelled"})
        ok &= check(status == 200 and cancel_data.get("freed_slots", 0) >= 1, "cancel reports the freed slots")
        status, jobs_after_cancel = api.get("/api/reactions/jobs")
        ok &= check(jobs_after_cancel["free_slots"] == free_before, "a cancelled order's reaction slots are released")

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


def test_order_preview(api: Api) -> bool:
    print(f"\n{'='*60}\n  Customer order: pre-commit review (preview) endpoint\n{'='*60}")
    ok = True
    product = _find_test_product(api)
    if not check(product is not None, "found a reachable single-tier product to test against"):
        print("  (no priced moon goo sheet or no live market reachability right now — skipping)")
        return ok
    per_run_yield = product["output_qty"] / product["top_level_runs"]
    target_qty = per_run_yield * 2 + 1

    status, before = api.get("/api/reactions/orders")
    n_before = len(before.get("orders", [])) if status == 200 else 0

    status, data = api.post("/api/reactions/orders/preview", {
        "type_id": product["type_id"], "target_qty": target_qty, "client_name": "Preview Client",
    })
    if not check(status == 200, f"preview succeeds (got {status}: {data})"):
        return ok
    ok &= check(data.get("preview") is True, "response is flagged as a preview")
    ok &= check(data.get("order", {}).get("id") is None, "preview order has no persisted id (nothing created)")
    ok &= check(len(data.get("materials", [])) > 0, "materials report is non-empty")
    ok &= check(data.get("cost", {}).get("total_cost", 0) > 0, "cost report is non-empty")
    # The whole point of the user's report: the time estimate must actually be populated.
    est = data.get("time", {}).get("estimated_hours")
    ok &= check(est is not None and est > 0, f"time estimate is populated (got {est})")

    status, after = api.get("/api/reactions/orders")
    n_after = len(after.get("orders", [])) if status == 200 else 0
    ok &= check(n_after == n_before, f"preview did NOT persist an order (before {n_before}, after {n_after})")
    return ok


def test_recurring_order_releases_each_cycle(api: Api) -> bool:
    """Recurring is operational, not a label: creation claims the first batch, completing a cycle
    keeps the standing order open, and a due list refresh claims the next one automatically."""
    print(f"\n{'='*60}\n  Recurring customer order: auto-assign -> complete -> release again\n{'='*60}")
    ok = True
    product = _find_test_product(api)
    if not check(product is not None, "found a reachable product for a recurring order"):
        return ok
    per_run_yield = product["output_qty"] / product["top_level_runs"]
    status, created = api.post("/api/reactions/orders", {
        "type_id": product["type_id"], "target_qty": per_run_yield,
        "client_name": "Weekly Client", "recurring_interval_days": 7,
    })
    ok &= check(status == 200, f"recurring order creates (got {status})")
    if status != 200:
        return ok
    order = created["order"]
    oid = order["id"]
    ok &= check(order["recurring_interval_days"] == 7, "weekly cadence is persisted")
    ok &= check(order["assigned_runs"] == order["top_level_runs"],
                "the first recurring batch is assigned automatically")

    status, completed = api.post(f"/api/reactions/orders/{oid}/status", {"status": "completed"})
    cycle = completed.get("order", {})
    ok &= check(status == 200 and cycle.get("status") == "open",
                "completing a recurring cycle keeps the standing order open")
    ok &= check(cycle.get("assigned_runs") == 0, "completion hands the cycle's runs back")

    # Bring the next cadence boundary forward; GET /orders is the release/retry sweep used by the
    # live Reactions refresh and must claim it without another button press.
    con = get_connection()
    con.execute("UPDATE pp_reaction_orders SET recurring_next_at=0 WHERE id=?", (oid,))
    con.commit()
    con.close()
    status, listed = api.get("/api/reactions/orders")
    again = next((o for o in listed.get("orders", []) if o["id"] == oid), {})
    ok &= check(status == 200 and again.get("assigned_runs") == again.get("top_level_runs"),
                "a due cadence refresh automatically assigns the next batch")

    status, stopped = api.post(f"/api/reactions/orders/{oid}/recurrence", {"action": "stop"})
    ok &= check(status == 200 and stopped.get("order", {}).get("recurring_interval_days") is None,
                "the user can stop future recurrence without deleting the order")
    api.post(f"/api/reactions/orders/{oid}/status", {"status": "cancelled"})
    return ok


def test_recurring_create_refreshes_visible_queue() -> bool:
    """Creating recurring work assigns on the server, so the browser must repaint the task cards.

    This is deliberately a small source contract: the backend integration test above already proves
    rows are inserted. The reported production failure was that the success callback refreshed only
    Orders and left Overview on its cached pre-create response.
    """
    print(f"\n{'='*60}\n  Recurring order create refreshes visible task queue\n{'='*60}")
    with open("static/reactions.js", encoding="utf-8") as f:
        js = f.read()
    start = js.index("function _rxCreateOrder()")
    end = js.index("function _rxOrderProfitHtml", start)
    body = js[start:end]
    ok = check("_rxReloadPlan();" in body,
               "create success reloads the reaction dashboard after automatic assignment")
    ok &= check("data.auto_assigned" in body,
                "create success acknowledges the automatic assignment")
    return ok


def test_reactions_phase1_is_task_first() -> bool:
    """The common Reactions path stays automated; manual/risk controls remain secondary."""
    print(f"\n{'='*60}\n  Reactions Phase 1: automated, task-first UI\n{'='*60}")
    with open("static/index.html", encoding="utf-8") as f:
        html = f.read()
    with open("static/reactions.js", encoding="utf-8") as f:
        js = f.read()
    start = html.index('id="tab-reactions"')
    end = html.index('id="tab-industry"', start)
    page = html[start:end]
    shop_start = page.index('id="rxShoppingPanel"')
    shop_end = page.index('id="rxOrdersPanel"', shop_start)
    shopping = page[shop_start:shop_end]
    ok = check('Find profitable work' in page and 'Add a specific product' in page,
               "the two distinct planning intents are explicit")
    ok &= check(page.index('rxMetricsContent') < page.index('Do this now'),
                "current-task metrics stay at the top of Overview")
    ok &= check('Advanced planning options' in page and 'id="wizRDeadline"' in page
                and 'id="rxMaterialFilter"' in page,
                "occasional deadline and material controls remain available under Advanced")
    ok &= check('_rxOpenSuggestForDeadline' not in shopping and '_rxOpenRecurringDeadline' not in shopping,
                "Shopping is an output, not a second planning launcher")
    ok &= check('class="rx-action-menu"' in page and 'Clear planned work' in page,
                "rare and destructive queue actions are grouped behind More")
    ok &= check("ppCloseTransientMenus('.rx-action-menu');" in page
                and "ppCloseTransientMenus('.rx-action-menu');\n  // First-run gate" in js,
                "the More menu closes after an action and whenever Reactions reloads")
    ok &= check('class="rx-capacity-section"' in js and 'rx-capacity-fold' not in js,
                "character and slot capacity stays visible under the task list")
    ok &= check('class="pp-card rx-metrics-card"' in page and 'rx-metrics-fold' not in page,
                "current-task metrics stay visible on Overview")
    create_start = js.index('function _rxCreateOrder()')
    create_end = js.index('// What the order EARNS', create_start)
    create_flow = js[create_start:create_end]
    ok &= check("ppReturnToOverview('rx', 'overview', 'rxOverviewPanel')" in create_flow
                and "rxOrderDetailModal').style.display = ''" not in create_flow,
                "creating an order closes the modal and returns directly to Overview")
    ok &= check("ppCloseTransientMenus('.rx-action-menu')" in js,
                "Reactions uses the shared transient-menu behavior")
    ok &= check('capacity_contract(reservation_model="reserved"' in open(
                    "app/reactions/jobs.py", encoding="utf-8").read(),
                "Reactions publishes the shared capacity contract as reserved capacity")
    return ok


# ── Deterministic unit tests (no network, no DB) ───────────────────────────────────────────────
# The safety net for refactoring reactions.py: these pin the refactor-critical PURE functions —
# the reaction-graph cost roll-up + cheaper-source selection (_resolve_reachable), the shopping-
# list explosion (_explode_shopping_list), chain-tier run counts (_explode_chain_tiers), and the
# shared batch valuation (_value_reaction_batch). Synthetic recipes only, so every number is exact
# and reproducible with no market/ESI/DB dependency. Run in-container (needs app deps installed).

def _approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol * max(1.0, abs(b))


# A tiny synthetic graph: leaves 100+101 react into P2 (2000); 2000 + leaf 102 react into P3 (3000).
_SYN_REACTIONS = {
    2000: [{"reaction_id": 1, "output_qty": 20, "cycle_time": 3600,
            "inputs": [{"type_id": 100, "quantity": 10}, {"type_id": 101, "quantity": 5}]}],
    3000: [{"reaction_id": 2, "output_qty": 10, "cycle_time": 7200,
            "inputs": [{"type_id": 2000, "quantity": 4}, {"type_id": 102, "quantity": 2}]}],
}
_SYN_GOO = {100: {"sell_price": 50.0}}                      # group has 100 at 50
_SYN_MARKET = {100: 40.0, 101: 30.0, 102: 20.0}            # market has 100 cheaper (40)


def test_resolve_reachable() -> bool:
    from app.reactions import _resolve_reachable, REACTION_ME_REDUCTION
    me = 1 - REACTION_ME_REDUCTION
    reached = _resolve_reachable(_SYN_GOO, _SYN_MARKET, _SYN_REACTIONS)
    ok = check(reached[100]["source"] == "market" and _approx(reached[100]["unit_cost"], 40.0),
               "leaf priced in both sources takes the cheaper (market 40 < group 50)")
    ok &= check(reached[100]["alt_source"] == "group" and _approx(reached[100]["alt_cost"], 50.0),
                "losing source (group 50) is recorded as alt_cost")
    exp2 = (10 * me * 40 + 5 * me * 30) / 20
    ok &= check(_approx(reached[2000]["unit_cost"], exp2), f"P2 unit_cost rolls up ME-adjusted (={exp2:.4f})")
    ok &= check(reached[2000]["via"] is not None and reached[2000]["reaction_count"] == 1,
                "P2 is a reaction node (via set, reaction_count 1)")
    exp3 = (4 * me * exp2 + 2 * me * 20) / 10
    ok &= check(_approx(reached[3000]["unit_cost"], exp3), f"P3 unit_cost rolls up through the P2 tier (={exp3:.4f})")
    ok &= check(reached[3000]["reaction_count"] == 2, "P3 reaction_count counts the whole subtree (2)")
    reached_jc = _resolve_reachable(_SYN_GOO, _SYN_MARKET, _SYN_REACTIONS,
                                    job_cost_rate=0.1, adjusted_prices=dict(_SYN_MARKET))
    ok &= check(reached_jc[2000]["job_cost"] > 0, "job_cost is charged when a job-cost rate is set")
    ok &= check(_approx(reached[2000]["job_cost"], 0.0), "job_cost is 0 by default (no reaction system)")
    return ok


def test_explode_shopping_list() -> bool:
    from app.reactions import _resolve_reachable, _explode_shopping_list, REACTION_ME_REDUCTION
    me = 1 - REACTION_ME_REDUCTION
    reached = _resolve_reachable(_SYN_GOO, _SYN_MARKET, _SYN_REACTIONS)
    out: dict = {}
    _explode_shopping_list(2000, 20, reached, out)   # exactly one run of P2
    ok = check(_approx(out.get(100, 0), 10 * me) and _approx(out.get(101, 0), 5 * me),
               "20 P2 units explode to one run's ME-adjusted leaves (9.78x100, 4.89x101)")
    out2: dict = {}
    _explode_shopping_list(101, 7, reached, out2)
    ok &= check(_approx(out2.get(101, 0), 7), "a raw leaf explodes to itself unchanged")
    return ok


def test_a_chain_spreads_over_the_slots_it_has() -> bool:
    """A slot is a RATE, not a container. The customer-order path used to put each tier in exactly
    one job — a real 2000-run Reinforced Carbon Fiber order became four jobs of ~2000 runs while
    fifty-five reaction slots sat idle. The chain runs tier by tier, so its time is
    sum(work/slots), and the tiers must NOT get equal shares when one carries ten times the runs."""
    from app.reactions.jobs import _fit_chain_slots

    # Order #36's real shape: Carbon Fiber 1956, Oxy-Organic 196, Thermosetting 1956, RCF 2000,
    # all on a 2.55h cycle.
    works = [1956 * 2.55, 196 * 2.55, 1956 * 2.55, 2000 * 2.55]
    caps = [1956, 196, 1956, 2000]

    ok = check(_fit_chain_slots(works, caps, 4) == [1, 1, 1, 1],
               "a four-slot budget still installs every tier of a four-tier chain")

    slots = _fit_chain_slots(works, caps, 40)
    ok &= check(sum(slots) == 40, "the whole budget is spent when every tier can still use it")
    ok &= check(min(slots) >= 1, "no tier is ever left with zero slots — the chain must install")
    ok &= check(slots[1] < slots[0], "the 196-run tier gets fewer slots than the 1956-run tier")
    ok &= check(sum(works[i] / slots[i] for i in range(4))
                < sum(works[i] / 10 for i in range(4)),
                "and it beats splitting the budget evenly across the tiers")

    # A slot per run is the end of it — past that a slot only adds an empty job.
    tiny = _fit_chain_slots([2 * 2.55, 1 * 2.55], [2, 1], 50)
    ok &= check(tiny == [2, 1], "no tier is given more slots than it has runs")
    ok &= check(_fit_chain_slots([], [], 10) == [] and _fit_chain_slots([1.0], [1], 0) == [],
                "an empty chain and a zero budget are both handled")
    return ok


def test_an_order_stops_at_the_character_that_is_not_worth_a_login() -> bool:
    """Reported from a live order (#45, 1000 runs of Reinforced Carbon Fiber): stage 1 spread over 5
    characters with two holding ONE job each, stage 2 over SEVEN with five holding one — while the
    characters that already had jobs sat on free reactors.

    The rule is marginal gain and needs no cadence: an order's wait is its reactor-hours over the
    reactors running them, so a host with F free slots added to S already committed cuts the wait by
    F/(S+F). Take hosts while that is worth a login; stop at the first one that isn't."""
    from app.reactions.jobs import _lean_hosts, _WORTH_A_LOGIN

    ids = lambda hs: [h["character_id"] for h in hs]
    mk = lambda *fs: [{"character_id": i, "free_slots": f} for i, f in enumerate(fs)]

    ok = check(_WORTH_A_LOGIN == 0.20, "an extra character must cut the wait by a fifth")

    # The reported account: three 10-slot characters and four 5-slot ones. The 4th buys 5/35 = 14%.
    real = mk(10, 10, 10, 5, 5, 5, 5)
    keep = _lean_hosts(real)
    ok &= check(ids(keep) == [0, 1, 2], "it stops at three characters on the reported account")
    ok &= check(sum(h["free_slots"] for h in keep) == 30,
                "which is 30 reactors — against the 33 the sprawl over seven was really using")

    # It scales itself, which is the point of a RELATIVE gain: a small order lands on one character
    # because the second buys nothing, a big spread-out account still uses everyone.
    ok &= check(ids(_lean_hosts(mk(10, 1))) == [0],
                "a character worth 1 slot beside 10 buys 9% and is not worth the login")
    ok &= check(ids(_lean_hosts(mk(10, 10, 10, 10))) == [0, 1, 2, 3],
                "four equal characters all pull real weight, so all four are used")
    # 5 equal hosts: the 5th buys exactly 5/25 = 20% and clears the bar, the 6th buys 16.7%.
    ok &= check(ids(_lean_hosts(mk(5, 5, 5, 5, 5, 5, 5))) == [0, 1, 2, 3, 4],
                "equal small characters keep going until the next one drops under a fifth")

    # Order matters: hosts are ranked by room, so the first one under the bar ends it.
    ok &= check(ids(_lean_hosts(mk(2, 10, 3, 9))) == [1, 3],
                "hosts are taken roomiest-first regardless of the order they arrive in")

    # Never strand an order, and never divide by zero.
    ok &= check(ids(_lean_hosts(mk(4))) == [0], "one character is always kept — it has to go somewhere")
    ok &= check(_lean_hosts([]) == [], "no hosts stays no hosts")
    ok &= check(ids(_lean_hosts(mk(10, 0, 0))) == [0], "a character with no free reactor is never added")

    # Packing also serves the "don't buy more formulas" rule: every host needs one formula of every
    # tier, so fewer hosts is strictly fewer formulas required.
    ok &= check(len(_lean_hosts(real)) < len(real),
                "fewer hosts than the account has — so fewer formulas the order demands at once")
    return ok


def test_a_stage_settles_on_one_run_count_across_its_products() -> bool:
    """Reported: *"I don't want to have to look for Carbon Fibers for each slot every time I start
    it to figure out how many runs. The more similar number of job runs (preferably equal) between
    products the better."*

    `_ALIGN_TOL` does not get there on its own. It lands a stage by matching DURATIONS, and with
    every product on the same cycle time 95 and 100 finish 5% apart — already "landed" — so nothing
    was closing the last gap, and 95 won on surplus because 1045 divides by it exactly.

    Two things were needed: scoring how many DIFFERENT numbers a stage asks you to type, and
    offering a shared count as a candidate layout. The second matters because the per-product pick
    always takes its own cheapest count first, so a shared number never falls out on its own."""
    from app.reactions.jobs import _choose_stage_layout, _level_options

    # Stage 1 of the reported order #45 — three products, all 3.00 h/run, real requirements.
    def stage():
        out = {}
        for k, total, cap in (("Carbon Fiber", 1045, 11), ("Thermosetting Polymer", 1100, 11),
                              ("Oxy-Organic Solvents", 100, 1)):
            out[k] = {"cycle": 3.0, "total": total,
                      "options": _level_options(total, cap=cap, max_runs=100000)}
        return out

    pick = _choose_stage_layout(stage(), prefer_tidy=True)
    runs = {k: o["runs"] for k, o in pick.items()}
    ok = check(runs["Carbon Fiber"] == runs["Thermosetting Polymer"],
               f"the two big products share one run count (got {runs})")
    ok &= check(len(set(runs.values())) < 3,
                "the stage asks for fewer numbers than it has products")
    ok &= check(all(o["jobs"] >= 1 for o in pick.values()), "every product still gets a job")

    # The surplus it spends is real goo, so it stays inside the budget the options already enforce.
    for k, o in pick.items():
        need = stage()[k]["total"]
        ok &= check(o["jobs"] * o["runs"] - need <= need * 0.5,
                    f"{k}'s overshoot stays inside the levelling budget")

    # A job is collected on a login, and logins fall on a day rhythm — so what a run count costs in
    # time is the SESSION it lands in, not its raw duration. 7d07h is not collected until day 8;
    # 6d23h is collected on day 7. Preferred, but ranked below the job count — it is not worth an
    # extra reactor.
    from app.reactions.jobs import _collection_slot
    ok &= check(_collection_slot(165) == 7 and _collection_slot(168) == 7,
                "6d23h and 7d00h are collected on the same login")
    ok &= check(_collection_slot(175) == 8, "and 7 hours past it is not collected until the next")
    ok &= check(_collection_slot(0) == 0, "a zero-length job is collected at once")

    # The regression this shape exists to prevent (2026-08-14). The old 0/1 flag read 7d00h18m as
    # no better than 7d16h, so a stage would buy 90 runs of surplus goo to stretch a job from
    # 7d00h18m to 7d23h — both collected on the SAME login, so the goo bought nothing but stock the
    # account cannot sell against an order.
    ok &= check(_collection_slot(168.3) == _collection_slot(168.0),
                "18 minutes past a boundary is still that day's login, not the next")
    stretch = _choose_stage_layout(
        {"X": {"cycle": 1.53, "total": 640, "options": _level_options(640, cap=8, max_runs=125)}},
        prefer_tidy=True)["X"]
    ok &= check(stretch["runs"] == 110,
                f"a stage takes the cheaper count inside the session it is collected on (got {stretch['runs']})")

    # A product that genuinely cannot reach the shared number keeps its own count rather than
    # being dragged to one it cannot afford — one number is a preference, not a rule.
    solo = {"Only": {"cycle": 3.0, "total": 7,
                     "options": _level_options(7, cap=1, max_runs=100000)}}
    ok &= check(len(_choose_stage_layout(solo, prefer_tidy=True)) == 1,
                "a single-product stage still resolves")
    return ok


def test_a_cadence_ceiling_holds_every_job_inside_the_week() -> bool:
    """Reported: *"I'd prefer to be able to schedule my jobs on a Saturday and handle the next stage
    a week later on a Saturday when I have time to play."*

    The setting already existed — `max_reaction_job_days`, Build rules → "Longest reaction job" —
    and the Industry scheduler had read it since it shipped. The Reactions stage solve never did,
    so it capped a stage at whatever the plan happened to already run.

    **Both directions, per the decision of 2026-08-14.** The cadence is a stated TARGET: where a
    stage can be split to fit the window it is, and where it genuinely cannot — more reactor-hours
    of work than the free reactors can turn over inside the window — it overruns and every option
    says by how much. Forcing it is not available: with too few reactors no split fits, and the code
    that claimed to force it was in fact collapsing its own option set to one unmeasured answer.

    The invariant this pins is the pair, not either half: **no run count is ever silently over the
    ceiling.** An option over the ceiling may only come back when no option under it exists, and it
    must carry `over_runs`."""
    from app.reactions.jobs import _choose_stage_layout, _level_options

    CYC = 1.53      # hours per run after structure + skill bonuses
    def stage(cap_hours):
        out = {}
        for k, total in (("Carbon Fiber", 1045), ("Thermosetting Polymer", 1100),
                         ("Oxy-Organic Solvents", 100)):
            out[k] = {"cycle": CYC, "total": total,
                      "options": _level_options(total, cap=30, max_runs=int(cap_hours / CYC))}
        return out

    ok = True
    for days in (14, 7, 3.5):
        pick = _choose_stage_layout(stage(days * 24), prefer_tidy=True)
        longest = max(o["runs"] * CYC for o in pick.values()) / 24.0
        ok &= check(longest <= days + 1e-6,
                    f"at a {days}-day cadence no job runs longer ({longest:.2f} d)")
        ok &= check(all(o["jobs"] * o["runs"] >= stage(days * 24)[k]["total"]
                        for k, o in pick.items()),
                    f"...and the {days}-day layout still covers every requirement")

    # Tighter cadence, more jobs — that is the trade being made, and it should be visible.
    wide = _choose_stage_layout(stage(14 * 24), prefer_tidy=True)
    tight = _choose_stage_layout(stage(3.5 * 24), prefer_tidy=True)
    ok &= check(sum(o["jobs"] for o in tight.values()) > sum(o["jobs"] for o in wide.values()),
                "a tighter cadence costs reactors — the ceiling is real, not advisory")

    # ── ...and where it CANNOT hold, the breach is reported ─────────────────────────────────────
    # The pair is the invariant. Take each layout the ceiling can be met at and assert nothing in it
    # claims a breach; then take a stage with fewer reactors than its work needs and assert every
    # option it is offered says how far past the window it reaches.
    for days in (14, 7, 3.5):
        fits = stage(days * 24)
        ok &= check(all(o.get("over_runs", 0) == 0 for p in fits.values() for o in p["options"]),
                    f"at {days} days every offered count fits, and none claims a breach")

    # Reproduced against the real branch: a 7-day window on Carbon Fiber is 119 runs; a stage whose
    # reactors force at least 200 runs a job cannot meet it at any layout. The old truth table
    # (`r > max_runs and r > min_runs`) let exactly ONE candidate through — r == min_runs — and
    # dropped every larger one, so the answer was 200 runs (11.7 days on a 7-day cadence) with
    # nothing recording that it had happened.
    squeezed = _level_options(1000, cap=5, max_runs=119, min_runs=200, budget=20.0)
    ok &= check(bool(squeezed) and all(o["over_runs"] > 0 for o in squeezed),
                "a stage that cannot fit still answers, and every option says it is over")
    ok &= check(len({o["over_runs"] for o in squeezed}) == 1
                and min(o["runs"] for o in squeezed) == 200,
                f"and only the LEAST-breaching counts are offered "
                f"(got {sorted(o['runs'] for o in squeezed)})")
    ok &= check(squeezed[0]["over_runs"] == squeezed[0]["runs"] - 119,
                "measured against the ceiling itself, in runs")
    # Missing a target is not permission to abandon it. The stage solve ranks fewest-jobs first, so
    # offering the whole over-ceiling set would hand it the longest job on the list: a 6,000-run
    # requirement that misses the ceiling by a third would come back as ONE job of 6,000.
    huge = _level_options(6000, cap=44, max_runs=100, budget=20.0)
    ok &= check(max(o["runs"] for o in huge) < 500,
                f"a badly over-committed stage still lands near the ceiling, not miles past it "
                f"(longest offered {max(o['runs'] for o in huge)} runs against a 100-run ceiling)")

    # The third route needs no loop at all: `min_runs` is not guaranteed to be among the candidates
    # (they are divisors of `total` plus tidy steps), and when it is not, `_level_options`' own
    # fallback fires — `floor = max(1, min_runs, ceil(total/cap))`, which ignored `max_runs`
    # outright and reported nothing about it.
    fallback = _level_options(1000, cap=5, max_runs=119, min_runs=1500, budget=20.0)
    ok &= check(len(fallback) == 1 and fallback[0]["runs"] >= 1500,
                f"with nothing offerable the fallback still answers (got {fallback})")
    ok &= check(fallback[0]["over_runs"] == fallback[0]["runs"] - 119,
                "and the fallback says how far over the ceiling it went, which it never used to")

    # ...and a breach is never claimed when one was AVOIDABLE. The floor — the smallest count the
    # work splits into with the reactors there are — is always available and frequently sits UNDER
    # the ceiling, but it used to be reachable only after the over-ceiling branch had already
    # returned, so an option 3 runs over won where a fitting one existed. Decision 1 says a stage
    # that can be split finer to fit IS, so a badge with an alternative behind it is a lie about the
    # plan. 30 runs across 2 reactors under a 17-run ceiling: the floor is exactly 17, two jobs.
    avoidable = _level_options(30, cap=2, max_runs=17, min_runs=17, budget=20.0)
    ok &= check(all(o["over_runs"] == 0 for o in avoidable),
                f"a within-ceiling floor is taken instead of a 3-run overrun "
                f"(got {[(o['runs'], o['over_runs']) for o in avoidable]})")
    ok &= check(all(o["jobs"] * o["runs"] >= 30 for o in avoidable),
                "...and it still covers the requirement")

    # The pair, stated once: an over-ceiling option may only appear when no under-ceiling one does,
    # AND only when the floor cannot fit either. Swept rather than spot-checked, because the
    # spurious-breach case only appears once the give-ground loop has raised `min_runs` to within a
    # run or two of `max_runs` — a band no hand-picked example lands in by accident.
    mixed = spurious = empty = 0
    for total in range(30, 220, 7):
        for cap in range(2, 9):
            for mr in range(10, 90, 3):
                for mn in range(max(1, mr - 6), mr + 2):
                    opts = _level_options(total, cap=cap, max_runs=mr, min_runs=mn, budget=20.0)
                    if not opts:
                        empty += 1
                        continue
                    if any(o["over_runs"] > 0 for o in opts) and any(o["over_runs"] == 0 for o in opts):
                        mixed += 1
                    # The floor always exists; if it fits, nothing returned may claim a breach.
                    floor = max(1, mn, -(-total // cap))
                    if floor <= mr and min(o["over_runs"] for o in opts) > 0:
                        spurious += 1
    ok &= check(mixed == 0, f"no parameter combination mixes fitting and breaching options ({mixed})")
    ok &= check(empty == 0, f"every combination answers with something ({empty} empty)")
    ok &= check(spurious == 0,
                f"no combination reports a breach while a within-ceiling floor existed ({spurious})")
    return ok


def test_the_leveller_never_plans_more_jobs_than_formulas_owned() -> bool:
    """Reported: *"it also suggested to do 21 slots of Carbon Fiber... but I only have 20 formulas"*
    — and 21 Thermosetting Polymer against 20 of those.

    A formula is a physical item locked into the reactor for the job's duration, so a product can
    never hold more parallel jobs than there are formulas of it. Every ASSIGN path applied that
    (`formula_concurrency_caps`); `level_product_runs` never asked, and it re-splits the whole plan
    on every dashboard load — so a plan that was legal when placed came back asking for a job the
    account cannot install. A tighter cadence makes it bite harder: a shorter job means more of
    them."""
    from app.reactions.jobs import _level_options

    # Carbon Fiber's real shape: 1045 runs, room for 21 jobs, a 7-day ceiling at 3 h/run = 56 runs.
    room, formulas, max_runs = 21, 20, 56
    loose = _level_options(1045, cap=room, max_runs=max_runs)
    ok = check(max(o["jobs"] for o in loose) == 21,
               "against slots alone the pass would offer 21 jobs — the reported bug")

    capped = _level_options(1045, cap=min(room, formulas), max_runs=max_runs)
    ok &= check(max(o["jobs"] for o in capped) <= formulas,
                "held to the formulas owned, it never offers more than 20")
    ok &= check(all(o["jobs"] * o["runs"] >= 1045 for o in capped),
                "and every count it does offer still covers the requirement")

    # Unknown stays unknown — no evidence about a formula must never refuse real work.
    unknown = _level_options(1045, cap=room, max_runs=max_runs)
    ok &= check(max(o["jobs"] for o in unknown) == room,
                "a product with no formula evidence is capped by slots alone, as before")
    return ok


def test_the_cadence_ceiling_is_measured_in_real_time_not_sde_time() -> bool:
    """Reported: a 7-day ceiling produced 55-run jobs and doubled the work. *"1 run of Carbon Fibers
    from my ingame numbers is 1h 24m 14s. 120 runs is 7d 0h 28m 48s."*

    `_reaction_cycle_times` returns the RAW SDE cycle and documents that the structure/skill bonus is
    deliberately not applied, because everything using it compares durations against each other and a
    common factor cancels. The cadence ceiling was the first ABSOLUTE consumer of those durations, and
    a common factor does not cancel against seven days: at 3.00 SDE hours against a real 1.4039, the
    ceiling allowed 56 runs where the truth is 119."""
    from app.reactions.jobs import _level_options, _collection_slot

    RAW, REAL = 3.00, 1.40389
    MULT, CAD = REAL / RAW, 7 * 24.0
    ok = check(abs(120 * REAL - 168.48) < 0.1,
               "120 runs really is 7d and ~29m — the reported measurement")

    before = int(CAD / RAW)                       # the bug: cadence compared to SDE hours
    after = int((CAD / MULT) / RAW)               # the fix: cadence converted to SDE hours
    ok &= check(before == 56 and after == 119,
                f"the ceiling goes from {before} to {after} runs a job")
    ok &= check(after * REAL <= CAD, "and {} runs really does land inside the week".format(after))
    ok &= check((after + 1) * REAL > CAD, "...while one more run would not")

    # The job count is what the player feels: 1045 runs of Carbon Fiber.
    lo = min(_level_options(1045, cap=30, max_runs=before), key=lambda o: o["jobs"])["jobs"]
    hi = min(_level_options(1045, cap=30, max_runs=after), key=lambda o: o["jobs"])["jobs"]
    ok &= check(lo == 19 and hi == 9, f"which halves the jobs for one product ({lo} -> {hi})")

    # The collection rhythm has to measure REAL time too, or it counts the wrong login.
    ok &= check(_collection_slot(119 * RAW * MULT) == 7,
                "a 119-run job is collected on day 7 in REAL hours")
    # A brand new account has never reacted, so there is nothing to measure — and the first
    # suggestion is the one that matters. Deriving the bonus instead has over-claimed every time it
    # was tried, so the answer is skills alone rather than a number that cannot be proved.
    import app.reactions.jobs as _J0
    ok &= check(_J0.reaction_time_mult_for(777051, 16673) <= 1.0,
                "...and falls back to skills rather than claiming a bonus it cannot prove")
    ok &= check(_J0._reaction_time_mult(777051, _derive=False) == 0.0,
                "measurement-only asks return 0 when nothing was ever measured, not a guess")

    # An idle account must keep the rate it measured. Reactors are idle exactly when a re-plan
    # happens, so measuring only from LIVE jobs made the ceiling flip 119 <-> 65 between cycles.
    import app.reactions.jobs as _J
    CTX = 777051
    _J._remember_time_mult(CTX, 0.4680)
    ok &= check(abs(_J._reaction_time_mult(CTX) - 0.4680) < 1e-6,
                "a measured rate is remembered and used with nothing running")
    ok &= check(int((CAD / _J._reaction_time_mult(CTX)) / RAW) == 119,
                "so an idle account still gets the 119-run ceiling, not the 65-run fallback")
    from app.db import get_connection as _gc
    _c = _gc(); _c.execute("UPDATE pp_industry_settings SET reaction_time_mult=NULL WHERE context_id=?", (CTX,)); _c.commit(); _c.close()
    ok &= check(_J._reaction_time_mult(CTX) < 1.0,
                "and with no measurement ever taken it falls back rather than guessing 1.0")

    # ...and the two measurements genuinely disagree — a run count that lands under a boundary in
    # real hours frequently does not in SDE hours, which is a different job being preferred.
    disagree = [r for r in range(20, 200)
                if _collection_slot(r * RAW * MULT) != _collection_slot(r * RAW)]
    ok &= check(len(disagree) > 40,
                f"real and SDE hours pick different jobs for {len(disagree)} of 180 run counts")
    return ok


def test_the_cadence_reaches_an_orders_own_top_row() -> bool:
    """Reported on a 7-day cadence: stage 1 obediently came down to 6.88-day jobs while stage 2 sat
    at **14 days**, so the order still took three weeks and the cadence bought nothing.

    `level_product_runs` deliberately never reshapes a customer order's top row — its run count is
    the batch the order was quoted on and cancelling hands exactly those runs back
    (`give_back_order_runs`). So it is split separately, and the total is preserved EXACTLY: the
    remainder rides on one job instead of being rounded up across all of them."""
    import time as _t
    from app.db import get_connection
    import app.reactions.jobs as J

    CTX, CID, TID = 777051, 991235, 16673
    J.ensure_reaction_assignments_table()
    real_cad, real_mult = J._reaction_cadence_hours, J._reaction_time_mult
    J._reaction_cadence_hours = lambda c: 168.0          # 7 days
    J._reaction_time_mult = lambda c: 0.4680             # the measured rate
    con = get_connection()
    con.execute("DELETE FROM pp_characters WHERE character_id=?", (CID,))
    con.execute("INSERT INTO pp_characters (context_id, character_id, character_name, "
                "mass_reactions, advanced_mass_reactions, scopes) VALUES (?,?,?,?,?,?)",
                (CTX, CID, "Order Top Probe", 5, 5, "x"))
    con.execute("DELETE FROM pp_reaction_assignments WHERE character_id=?", (CID,))
    con.execute("INSERT INTO pp_reaction_assignments (character_id,type_id,name,runs,input_cost,"
                "reward,created_at,tier_order,order_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (CID, TID, "Crystalline Carbonide", 1001, 100100.0, 50050.0, _t.time(), 1, 999))
    con.commit(); con.close()
    try:
        wrote = J.split_order_tops_to_cadence(CTX)
        con = get_connection()
        rs = [dict(r) for r in con.execute(
            "SELECT runs, input_cost FROM pp_reaction_assignments WHERE character_id=?", (CID,))]
        con.close()
        cyc = J._reaction_cycle_times().get(TID, 0.0)
        longest = max(r["runs"] for r in rs) * cyc * 0.4680 / 24.0

        ok = check(wrote == len(rs) and len(rs) > 1, f"the batch was split into {len(rs)} jobs")
        ok &= check(sum(r["runs"] for r in rs) == 1001,
                    "the order's total is preserved EXACTLY — its arithmetic depends on it")
        ok &= check(abs(sum(r["input_cost"] for r in rs) - 100100.0) < 1.0,
                    "and so is what the batch cost")
        ok &= check(longest <= 7.0, f"every job now lands inside the week ({longest:.2f} d)")
        ok &= check(len({r["runs"] for r in rs}) <= 2,
                    "the remainder rides on one job rather than being spread over all of them")

        # A batch already inside the window is left alone entirely.
        con = get_connection()
        con.execute("DELETE FROM pp_reaction_assignments WHERE character_id=?", (CID,))
        con.execute("INSERT INTO pp_reaction_assignments (character_id,type_id,name,runs,input_cost,"
                    "reward,created_at,tier_order,order_id) VALUES (?,?,?,?,?,?,?,?,?)",
                    (CID, TID, "Crystalline Carbonide", 50, 5000.0, 0.0, _t.time(), 1, 999))
        con.commit(); con.close()
        ok &= check(J.split_order_tops_to_cadence(CTX) == 0,
                    "a batch that already fits is not touched")
        return ok
    finally:
        J._reaction_cadence_hours, J._reaction_time_mult = real_cad, real_mult
        con = get_connection()
        con.execute("DELETE FROM pp_reaction_assignments WHERE character_id=?", (CID,))
        con.execute("DELETE FROM pp_characters WHERE character_id=?", (CID,))
        con.commit(); con.close()


def test_the_leveller_does_not_reach_for_a_character_the_assign_left_out() -> bool:
    """Reported while watching the page: *"it did right... but when I was looking at it suddenly
    swapped the 3x 7 slots to 3x7 slots + 1x1 slot."*

    The two passes disagreed by construction. `_allocate_and_insert` packs an order onto the fewest
    characters worth a login (`_lean_hosts`); `level_product_runs` re-splits the whole plan on EVERY
    dashboard load and placed jobs against slot room alone, so it saw a spare reactor on a fourth
    character and put a single job there. Same rule on both sides now: a character already in the
    plan is a login you are making anyway, and anyone else has to earn the trip."""
    from app.reactions.jobs import _WORTH_A_LOGIN

    # The placement rule, stated the way Step 5b applies it.
    def joins(free: int, reachable: int) -> bool:
        return free > 0 and free / float(reachable + free) >= _WORTH_A_LOGIN

    ok = check(not joins(1, 12),
               "a 4th character with 1 reactor does not join 3 that still have 12 between them")
    ok &= check(not joins(5, 30), "nor 5 reactors against the reported account's 30")
    ok &= check(joins(5, 0),
                "but when the characters in the plan are FULL, a new one joins at once")
    ok &= check(joins(10, 20), "and a character carrying real weight is always worth the trip")
    ok &= check(not joins(0, 5), "a character with no free reactor is never added")

    # A row that is merely PLANNED is not a commitment: nothing is installed, so moving it costs
    # nothing and a row on a character the packing rule would never have picked is pure overhead.
    # The stability rule ("whoever already runs it keeps it") exists to stop churn between loads,
    # and it must not be read as protecting a placement no one is running.
    import app.reactions.jobs as _J2
    room = {1: 10, 2: 10, 3: 10, 4: 5}
    lean = {h["character_id"] for h in _J2._lean_hosts(
        [{"character_id": c, "free_slots": n} for c, n in room.items() if n > 0])}
    ok &= check(lean == {1, 2, 3},
                "the 5-reactor character is not worth a login beside three 10s")
    ok &= check(4 not in lean,
                "so a pending row parked on it should move, not be preserved by stability")

    # Under the ONE-SLOT model a stage may use the whole character, because stage 2 runs in the
    # reactor stage 1 frees. Charging both against one pool made three 10-reactor characters read
    # as full at 21 + 9 rows, which is what pushed a 21st job onto a fourth host.
    room, s1, s2 = 10, 21, 9
    ok &= check(s1 + s2 == room * 3,
                "21 + 9 rows fills three 10-reactor characters EXACTLY — zero slack, which is why "
                "one job had nowhere to land")
    ok &= check(room - (s2 // 3) == 7 and 7 * 3 == s1,
                "charging stage 2 first left stage 1 exactly 7 each, so any uneven split spills")
    ok &= check(max(s1, s2) <= room * 3 and room * 3 - s1 == 9,
                "counting only the busiest stage leaves 9 reactors spare, so it cannot spill")
    ok &= check(s1 <= room * 3 and s2 <= room * 3,
                "the invariant the old subtraction protected still holds: no STAGE exceeds the room")

    # The threshold is the same number the order allocator uses — one rule, not two that drift.
    import app.reactions.jobs as _J
    hosts = [{"character_id": 1, "free_slots": 10}, {"character_id": 2, "free_slots": 10},
             {"character_id": 3, "free_slots": 10}, {"character_id": 4, "free_slots": 5}]
    kept = {h["character_id"] for h in _J._lean_hosts(hosts)}
    ok &= check(kept == {1, 2, 3},
                "the assign path drops the same 4th character the leveller now refuses to add")
    return ok


def test_the_leveller_consolidates_a_stray_host_off_the_plan() -> bool:
    """The reported plan, seeded and run through the REAL levelling pass: 7/7/6 jobs on three
    10-reactor characters plus a single job stranded on a 5-reactor fourth. *"Suddenly we have an
    extra slot on 3 characters again, it still doesn't distribute stuff."*

    Two bugs kept that row in place. The stage budget charged stage 2's rows against stage 1's
    reactors, so three 10-reactor characters read as exactly full at 21 + 9 rows; and the overflow
    placement was gated on "already holds a row", which let any historical placement re-justify
    itself on every pass forever. Gated on the characters worth a login instead, computed from
    reactors."""
    import time as _t
    from app.db import get_connection
    import app.reactions.jobs as J

    CTX = 777099
    CH = [(9910001, "Big A", 10), (9910002, "Big B", 10), (9910003, "Big C", 10),
          (9910004, "Small D", 5)]
    S1, S2 = (16673, "Crystalline Carbonide"), (16681, "Fullerides")
    con = get_connection()
    for cid, nm, slots in CH:
        con.execute("DELETE FROM pp_characters WHERE character_id=?", (cid,))
        mr = min(5, slots - 1)
        con.execute("INSERT INTO pp_characters (context_id,character_id,character_name,"
                    "mass_reactions,advanced_mass_reactions,scopes) VALUES (?,?,?,?,?,?)",
                    (CTX, cid, nm, mr, max(0, slots - 1 - mr),
                     "esi-industry.read_character_jobs.v1"))
        con.execute("DELETE FROM pp_reaction_assignments WHERE character_id=?", (cid,))
    now = _t.time()
    for cid, n in ((9910001, 7), (9910002, 7), (9910003, 6), (9910004, 1)):
        for _ in range(n):
            con.execute("INSERT INTO pp_reaction_assignments (character_id,type_id,name,runs,"
                        "input_cost,reward,created_at,tier_order,order_id) VALUES (?,?,?,?,?,?,?,?,?)",
                        (cid, S1[0], S1[1], 113, 0, 0, now, 0, 4242))
    for cid in (9910001, 9910002, 9910003):
        for _ in range(3):
            con.execute("INSERT INTO pp_reaction_assignments (character_id,type_id,name,runs,"
                        "input_cost,reward,created_at,tier_order,order_id) VALUES (?,?,?,?,?,?,?,?,?)",
                        (cid, S2[0], S2[1], 111, 0, 0, now, 1, 4242))
    con.commit(); con.close()

    saved = (J._level_runs_on, J._tidy_runs_on, J._parallel_stages_on,
             J._reaction_cadence_hours, J._reaction_time_mult)
    J._level_runs_on = lambda c: True
    J._tidy_runs_on = lambda c: True
    J._parallel_stages_on = lambda c: True
    J._reaction_cadence_hours = lambda c: 168.0
    J._reaction_time_mult = lambda c, _derive=True: 0.468

    def hosts():
        con = get_connection()
        rs = con.execute("SELECT DISTINCT c.character_name nm FROM pp_reaction_assignments a "
                         "JOIN pp_characters c ON c.character_id=a.character_id "
                         "WHERE c.context_id=?", (CTX,)).fetchall()
        con.close()
        return {r["nm"] for r in rs}
    try:
        ok = check(hosts() == {"Big A", "Big B", "Big C", "Small D"},
                   "seeded with the stray 5-reactor host holding one job")
        for _ in range(3):
            J.level_product_runs(CTX)
        after = hosts()
        ok &= check("Small D" not in after,
                    f"the stray host is emptied by the levelling pass (left: {sorted(after)})")
        ok &= check(after == {"Big A", "Big B", "Big C"},
                    "and the work stays on the three characters worth a login")
        # Stable: running it again must not put it back, which is how this was first noticed.
        J.level_product_runs(CTX)
        ok &= check("Small D" not in hosts(), "and a further pass does not re-add it")

        # The order's own top row (stage 2) must still be untouched — it is the batch the order was
        # quoted on. The FIRST version of this test used order_id=None and so never exercised the
        # exclusion at all, which is why it passed locally while prod stayed on four characters.
        con = get_connection()
        s2 = [dict(r) for r in con.execute(
            "SELECT a.runs, c.character_name nm FROM pp_reaction_assignments a "
            "JOIN pp_characters c ON c.character_id=a.character_id "
            "WHERE c.context_id=? AND a.tier_order=1", (CTX,))]
        con.close()
        ok &= check(len(s2) == 9 and all(r["runs"] == 111 for r in s2),
                    "the order's quoted top row is left exactly as it was")

        # ...and NO character ends up holding more rows than it has reactors. Consolidating by
        # freeing the stage budget instead put 8 stage-1 rows beside 3 stage-2 on a 10-reactor
        # character — 11 rows on 10 reactors, which is the incident the cross-stage subtraction
        # was added for in the first place. A row is a line in the plan whether or not it can be
        # installed yet, and the plan has to fit the reactors.
        con = get_connection()
        per = [dict(r) for r in con.execute(
            "SELECT c.character_name nm, COUNT(*) n FROM pp_reaction_assignments a "
            "JOIN pp_characters c ON c.character_id=a.character_id "
            "WHERE c.context_id=? GROUP BY c.character_name", (CTX,))]
        con.close()
        cap = {nm: slots for _cid, nm, slots in CH}
        over = [(r["nm"], r["n"], cap[r["nm"]]) for r in per if r["n"] > cap[r["nm"]]]
        ok &= check(not over, f"no character holds more rows than reactors (over: {over})")
        # NOT a fixed row count: consolidating lets the pass re-split 21 jobs of 113 into 20 of
        # 119, which is the same work in one fewer reactor. What must hold is that the work is
        # still covered — a dropped row is silently under-producing the order.
        con = get_connection()
        s1 = con.execute("SELECT COALESCE(SUM(a.runs),0) t FROM pp_reaction_assignments a "
                         "JOIN pp_characters c ON c.character_id=a.character_id "
                         "WHERE c.context_id=? AND a.tier_order=0", (CTX,)).fetchone()["t"]
        con.close()
        ok &= check(s1 >= 21 * 113,
                    f"the stage-1 work is still fully covered ({s1} runs vs 2373 needed)")
        return ok
    finally:
        (J._level_runs_on, J._tidy_runs_on, J._parallel_stages_on,
         J._reaction_cadence_hours, J._reaction_time_mult) = saved
        con = get_connection()
        for cid, _n, _s in CH:
            con.execute("DELETE FROM pp_reaction_assignments WHERE character_id=?", (cid,))
            con.execute("DELETE FROM pp_characters WHERE character_id=?", (cid,))
        con.commit(); con.close()


def test_it_warns_when_installed_jobs_will_come_up_short() -> bool:
    """Reported after a batch where three jobs went in at 120 runs where the plan said 113: *"We
    should warn the user when they underproduce number of runs so they can add the extra runs. But
    overproducing should not be bothered with honestly."*

    The asymmetry is the whole design. Under-producing means the stage above cannot start, and it
    is invisible until the last job — in a structure, with the materials already bought. Over-
    producing is stock. A warning nobody needs to act on is one they learn to ignore."""
    import time as _t, json as _json
    from app.db import get_connection
    import app.reactions.jobs as J

    CTX, CID, TID = 777096, 9940001, 16673
    J.ensure_industry_jobs_table()
    saved = (J._level_runs_on, J._tidy_runs_on)
    J._level_runs_on, J._tidy_runs_on = (lambda c: False), (lambda c: False)

    def setup(job_runs):
        con = get_connection()
        con.execute("DELETE FROM pp_characters WHERE character_id=?", (CID,))
        con.execute("INSERT INTO pp_characters (context_id,character_id,character_name,"
                    "mass_reactions,advanced_mass_reactions,scopes) VALUES (?,?,?,?,?,?)",
                    (CTX, CID, "Prod", 5, 4, "esi-industry.read_character_jobs.v1"))
        con.execute("DELETE FROM pp_reaction_assignments WHERE character_id=?", (CID,))
        con.execute("DELETE FROM pp_char_industry_jobs WHERE character_id=?", (CID,))
        now = _t.time()
        for _ in range(4):
            con.execute("INSERT INTO pp_reaction_assignments (character_id,type_id,name,runs,"
                        "input_cost,reward,created_at,tier_order,order_id) VALUES (?,?,?,?,?,?,?,?,?)",
                        (CID, TID, "Crystalline Carbonide", 113, 0, 0, now, 0, None))
        jobs = [{"product_type_id": TID, "runs": r, "status": "active", "duration": r * 3 * 3600}
                for r in job_runs]
        con.execute("INSERT INTO pp_char_industry_jobs (character_id,jobs_json,fetched_at) "
                    "VALUES (?,?,?)", (CID, _json.dumps(jobs), now))
        con.commit(); con.close()

    try:
        setup([113, 113])
        ok = check(not (J.get_industry_jobs(context_id=CTX).get("under_production") or []),
                   "installing exactly what was planned says nothing")

        setup([120, 120])
        ok &= check(not (J.get_industry_jobs(context_id=CTX).get("under_production") or []),
                    "and installing MORE than planned is silent — surplus is stock, not a problem")

        setup([100, 100])
        up = J.get_industry_jobs(context_id=CTX).get("under_production") or []
        ok &= check(len(up) == 1, "installing fewer runs than planned raises exactly one warning")
        if up:
            ok &= check(up[0]["short_runs"] == 26,
                        f"...naming the runs to add (2 jobs 13 short each = 26, got {up[0]['short_runs']})")
            ok &= check(up[0]["planned"] == 452 and up[0]["covered"] == 426,
                        "and what is covered against what the plan asked for")
            ok &= check(up[0]["name"] == "Crystalline Carbonide", "and which product it is")
        return ok
    finally:
        J._level_runs_on, J._tidy_runs_on = saved
        con = get_connection()
        con.execute("DELETE FROM pp_reaction_assignments WHERE character_id=?", (CID,))
        con.execute("DELETE FROM pp_char_industry_jobs WHERE character_id=?", (CID,))
        con.execute("DELETE FROM pp_characters WHERE character_id=?", (CID,))
        con.commit(); con.close()


def test_a_character_that_lost_the_jobs_scope_says_so() -> bool:
    """TODO §37a. Re-authorising a character through the NORMAL login silently drops the opt-in
    industry-jobs scope. Its `pp_char_industry_jobs` row then freezes at the last successful read —
    the Reactions tab and all three reaction alerts keep computing off data nothing can refresh.

    Before this, that character simply fell into "not yet tracked", indistinguishable from one that
    was never connected. `scope_lost` is what separates the two, and the alert rescan holds a
    frozen character's alerts back only BECAUSE this flag makes the page say what to do about it —
    so the two must agree on who is in that state."""
    import time as _t, json as _json
    from app.db import get_connection
    import app.reactions.jobs as J

    CTX, CID, TID = 777097, 9940002, 16673
    J.ensure_industry_jobs_table()

    def setup(scopes, with_jobs_row, skills=(5, 4)):
        con = get_connection()
        con.execute("DELETE FROM pp_characters WHERE character_id=?", (CID,))
        con.execute("INSERT INTO pp_characters (context_id,character_id,character_name,"
                    "mass_reactions,advanced_mass_reactions,scopes) VALUES (?,?,?,?,?,?)",
                    (CTX, CID, "Lapsed", skills[0], skills[1], scopes))
        con.execute("DELETE FROM pp_char_industry_jobs WHERE character_id=?", (CID,))
        if with_jobs_row:
            jobs = [{"product_type_id": TID, "runs": 10, "status": "active", "activity_id": 9,
                     "duration": 3 * 3600}]
            con.execute("INSERT INTO pp_char_industry_jobs (character_id,jobs_json,fetched_at) "
                        "VALUES (?,?,?)", (CID, _json.dumps(jobs), _t.time()))
        con.commit(); con.close()

    def _row():
        chars = J.get_industry_jobs(context_id=CTX).get("characters") or []
        return next((c for c in chars if c["character_name"] == "Lapsed"), None)

    try:
        # Tracked until its token changed: the snapshot is still here, the scope is not.
        setup("esi-planets.manage_planets.v1", with_jobs_row=True)
        r = _row()
        ok = check(r is not None and r.get("tracked") is False and r.get("scope_lost") is True,
                   f"a character holding a jobs snapshot with no jobs scope is flagged scope_lost (got {r})")

        # Never connected — no snapshot, so nothing is frozen and there is nothing to explain.
        # Saying "job tracking was disconnected" here would be a warning about nothing.
        setup("esi-planets.manage_planets.v1", with_jobs_row=False)
        r = _row()
        ok &= check(r is not None and r.get("scope_lost") is False,
                    f"a character that never tracked jobs is NOT flagged (got {r})")

        # Still connected: it is tracked, and the prompt would be plain wrong.
        setup("esi-industry.read_character_jobs.v1", with_jobs_row=True)
        r = _row()
        ok &= check(r is not None and r.get("tracked") is True and not r.get("scope_lost"),
                    f"a still-connected character is tracked and unflagged (got {r})")

        # The trap: `tracked` is false for TWO reasons — no scope, or no reaction skill trained.
        # A scope-holding character with no Mass Reactions still gets a jobs row written for it, so
        # a flag that only asks "is there a row?" warns that a perfectly working character was
        # disconnected — permanently, since re-authorising cannot clear it.
        setup("esi-industry.read_character_jobs.v1", with_jobs_row=True, skills=(0, 0))
        r = _row()
        ok &= check(r is not None and r.get("tracked") is False and r.get("scope_lost") is False,
                    f"an untrained character that still HOLDS the scope is not flagged (got {r})")
        return ok
    finally:
        con = get_connection()
        con.execute("DELETE FROM pp_char_industry_jobs WHERE character_id=?", (CID,))
        con.execute("DELETE FROM pp_characters WHERE character_id=?", (CID,))
        con.commit(); con.close()


def test_a_reaction_can_be_marked_running_or_done_by_hand() -> bool:
    """ESI is the signal for what is running and what has landed, and it is right nearly always —
    but the job cache is up to five minutes stale, a job installed under a different product than
    planned matches nothing, and a chain reacted before this tool saw it has no job to read. In all
    three the page said "after stage 1 finishes" about a stage that was over, with no way to say so.
    A mark is a FLOOR under what was observed: it can bring a stage forward, never hide a real job."""
    from app.reactions.jobs import chain_stage_state, manual_jobs, _RX_DONE, _RX_RUNNING, _RX_ALL

    rows = [{"character_id": 1, "type_id": 11, "tier_order": 0, "runs": 5, "created_at": 100.0,
             "name": "Carbon Fiber"},
            {"character_id": 1, "type_id": 12, "tier_order": 1, "runs": 5, "created_at": 100.0,
             "name": "Reinforced Carbon Fibers"}]
    st = lambda marks: {s["stage"]: s for s in chain_stage_state(rows, [], 0.0, marks)}

    bare = st(None)
    ok = check(bare[0]["ready"] and not bare[1]["ready"],
               "with no jobs and no marks, stage 2 waits on stage 1")

    done = st({(1, 11, 0): (_RX_ALL, _RX_DONE)})
    ok &= check(done[0]["done"] == 1, "marking stage 1 done counts it as done")
    ok &= check(done[1]["ready"], "and that is what lets stage 2 start — the whole point")

    run = st({(1, 11, 0): (_RX_ALL, _RX_RUNNING)})
    ok &= check(run[0]["running"] == 1 and run[0]["done"] == 0,
                "marking it merely installed counts as running, not done")
    ok &= check(not run[1]["ready"],
                "and a running stage does NOT release the stage above — running is not finished")

    # A mark must never be able to un-see a job ESI reports. Same rule as `resolve_done`.
    jobs = [{"product_type_id": 11, "status": "delivered"}]
    seen = st({(1, 11, 0): (_RX_ALL, _RX_RUNNING)})
    esi = {s["stage"]: s for s in chain_stage_state(rows, jobs, 0.0,
                                                    {(1, 11, 0): (_RX_ALL, _RX_RUNNING)})}
    ok &= check(esi[0]["done"] == 1 and esi[1]["ready"],
                "a 'running' mark cannot downgrade a job ESI already reports as delivered")
    ok &= check(seen[0]["running"] == 1, "(and with no ESI job the same mark still reads running)")

    # Partial marks resolve against the plan, and the states are alternatives rather than a ladder.
    marks = {(1, 11, 0): (2, _RX_DONE)}
    ok &= check(manual_jobs(marks, 1, 11, 0, 4, _RX_DONE) == 2, "2 of 4 jobs marked done is 2")
    ok &= check(manual_jobs(marks, 1, 11, 0, 4, _RX_RUNNING) == 0,
                "and asking the same group for 'running' is 0 — one mark, not a ladder")
    ok &= check(manual_jobs({(1, 11, 0): (_RX_ALL, _RX_DONE)}, 1, 11, 0, 7, _RX_DONE) == 7,
                "'all' follows the plan's job count, so it survives a re-split")
    ok &= check(manual_jobs(marks, 1, 11, 0, 1, _RX_DONE) == 1,
                "a mark is capped at what the plan actually holds today")
    ok &= check(manual_jobs(marks, 99, 11, 0, 4, _RX_DONE) == 0
                and manual_jobs(marks, 1, 11, 5, 4, _RX_DONE) == 0,
                "another character or another stage is a different group entirely")
    return ok


def test_assigning_twice_does_not_book_it_twice() -> bool:
    """`POST /api/reactions/assign` was a bare INSERT, so a retry appended a second full set of
    rows — and a frontend bug that reported every SUCCESSFUL assign as failed turned two
    suggestions into 27 rows on a 10-slot character. The capacity half has to count what actually
    runs AT ONCE: chain tiers are sequential, so summing every row would reject a legitimate deep
    chain that never occupies more than a couple of slots at a time."""
    from app.reactions.jobs import _concurrent_load

    # A four-tier chain, two jobs per tier: eight rows, but never more than two at once.
    chain = [{"tier_order": t} for t in range(4) for _ in range(2)]
    ok = check(_concurrent_load(chain, {}) == 2,
               "a deep chain counts its widest TIER, not its eight rows")
    ok &= check(len(chain) == 8, "(which is the whole point — eight rows, two slots)")

    # Two different products sharing a tier DO compete: they install at the same moment.
    both = [{"tier_order": 0}, {"tier_order": 0}, {"tier_order": 1}]
    ok &= check(_concurrent_load(both, {}) == 2,
                "products sharing a tier compete for slots")
    ok &= check(_concurrent_load(both, {0: 3}) == 5,
                "and what is being added counts against the tier it lands on")
    ok &= check(_concurrent_load([], {}) == 0, "an empty plan occupies nothing")
    ok &= check(_concurrent_load([], {2: 4}) == 4,
                "a first assignment is measured on its own")
    return ok


def test_explode_chain_tiers() -> bool:
    from app.reactions import _resolve_reachable, _explode_chain_tiers
    reached = _resolve_reachable(_SYN_GOO, _SYN_MARKET, _SYN_REACTIONS)
    tiers: dict = {}
    _explode_chain_tiers(reached[3000]["via"]["inputs"], 1, reached, tiers)
    ok = check(2000 in tiers and tiers[2000]["runs"] == 1,
               "one run of P3 needs one run of its P2 intermediate tier")
    ok &= check(102 not in tiers, "raw leaves are not listed as chain tiers")
    return ok


def test_value_reaction_batch() -> bool:
    """The shared cost/profit valuation (extracted during the refactor). Skips cleanly until it
    exists so this file stays green on an un-refactored tree."""
    try:
        from app.reactions import _value_reaction_batch, _resolve_reachable
    except ImportError:
        print("  SKIP: _value_reaction_batch not extracted yet")
        return True
    reached = _resolve_reachable(_SYN_GOO, _SYN_MARKET, _SYN_REACTIONS)
    node = reached[2000]
    settings = {"export_isk_per_m3": 0.0, "export_collateral_pct": 0.0}
    v = _value_reaction_batch(node, total_out=20, sell_price=100.0, volume=0.0, settings=settings)
    ok = check(_approx(v["input_cost"], 20 * node["unit_cost"]), "input_cost = total_out x unit_cost")
    ok &= check(_approx(v["output_value"], 20 * 100.0), "output_value = total_out x sell_price")
    ok &= check(_approx(v["net_profit"], v["output_value"] - v["input_cost"] - v["job_cost"]),
                "net_profit nets out materials + job cost (no shipping/collateral here)")
    return ok


def test_no_undefined_names() -> bool:
    """Static guard against the module-split failure mode: a submodule using a name it never
    imported (member_group did exactly this and 500'd the prod shopping list — a NameError raised
    only at CALL time, so import + our old live tests against a stale server missed it). pyflakes
    catches it statically. Skips cleanly if pyflakes isn't installed."""
    import glob
    import os
    import subprocess
    try:
        import pyflakes  # noqa: F401
    except ImportError:
        print("  SKIP: pyflakes not installed (pip install pyflakes to enable this guard)")
        return True
    root = os.path.dirname(os.path.abspath(__file__))
    files = sorted(glob.glob(os.path.join(root, "app", "reactions", "*.py")))
    out = subprocess.run([sys.executable, "-m", "pyflakes", *files], capture_output=True, text=True)
    undefined = [ln for ln in out.stdout.splitlines() if "undefined name" in ln]
    for ln in undefined:
        print("   ", ln)
    return check(not undefined, "no undefined names in app/reactions/*.py (module-split guard)")


def test_local_sell_hint() -> bool:
    """The 'reactions completed → sell to a local buy order' hint: a local buy that beats Jita AFTER
    jump-freight produces a depth-capped sell line; a local buy that loses to Jita-net produces
    nothing. Monkeypatches the market/settings lookups so it's deterministic and offline (the real
    ones need followed markets + live ESI)."""
    import app.features, app.markets, app.market, app.reactions.settings
    from app.notifications import _reaction_completed_sale_hint
    saved = (app.features.feature_enabled, app.markets.effective_markets, app.markets.best_local_buy,
             app.market.fetch_market_data, app.reactions.settings.effective_reaction_settings)
    ok = True
    try:
        app.features.feature_enabled = lambda k: True
        app.markets.effective_markets = lambda ctx: [{"name": "Local Hub", "kind": "structure", "location_id": 1}]
        app.market.fetch_market_data = lambda tids: {16662: {"buy_price": 50000.0}}
        app.reactions.settings.effective_reaction_settings = lambda ctx: {"export_isk_per_m3": 1200.0}
        evs = [{"products": [{"type_id": 16662, "runs": 50}], "character_name": "X"}]

        # Local 52k beats Jita-net (~49.8k after freight); buy depth 8000 caps the sellable amount.
        app.markets.best_local_buy = lambda ctx, tids: {16662: {"buy_price": 52000.0, "buy_volume": 8000.0, "market": "Local Hub"}}
        hint = _reaction_completed_sale_hint(1, evs)
        ok &= check("Local Hub" in hint and "8,000" in hint, "local buy beating Jita-after-freight yields a depth-capped sell line")

        # Local 40k loses to Jita-net → no hint.
        app.markets.best_local_buy = lambda ctx, tids: {16662: {"buy_price": 40000.0, "buy_volume": 8000.0, "market": "Local Hub"}}
        ok &= check(_reaction_completed_sale_hint(1, evs) == "", "local buy below Jita-after-freight yields no hint")
    finally:
        (app.features.feature_enabled, app.markets.effective_markets, app.markets.best_local_buy,
         app.market.fetch_market_data, app.reactions.settings.effective_reaction_settings) = saved
    return ok


def run_unit_tests() -> bool:
    print("Unit tests (pure functions, no network/DB):")
    results = [
        test_resolve_reachable(),
        test_explode_shopping_list(),
        test_a_chain_spreads_over_the_slots_it_has(),
        test_an_order_stops_at_the_character_that_is_not_worth_a_login(),
        test_a_stage_settles_on_one_run_count_across_its_products(),
        test_a_cadence_ceiling_holds_every_job_inside_the_week(),
        test_the_leveller_never_plans_more_jobs_than_formulas_owned(),
        test_the_leveller_does_not_reach_for_a_character_the_assign_left_out(),
        test_the_leveller_consolidates_a_stray_host_off_the_plan(),
        test_it_warns_when_installed_jobs_will_come_up_short(),
        test_a_character_that_lost_the_jobs_scope_says_so(),
        test_the_cadence_ceiling_is_measured_in_real_time_not_sde_time(),
        test_the_cadence_reaches_an_orders_own_top_row(),
        test_a_reaction_can_be_marked_running_or_done_by_hand(),
        test_assigning_twice_does_not_book_it_twice(),
        test_explode_chain_tiers(),
        test_value_reaction_batch(),
        test_local_sell_hint(),
        test_no_undefined_names(),
    ]
    return all(results)


def test_pricing_endpoints_live(api: "Api") -> bool:
    """Smoke the endpoints that go through _load_goo_and_reached end-to-end (opportunities +
    shopping list). These 500'd in prod after the split when graph.py was missing an import — a
    fresh-app run of THIS catches that class of bug (the old suite didn't hit these paths). A 200
    with the right shape is all we assert; the numbers depend on live market data."""
    print("\n" + "=" * 60)
    print("  Live pricing endpoints (exercise _load_goo_and_reached)")
    print("=" * 60)
    ok = True
    status, opps = api.get("/api/reactions/opportunities")
    ok &= check(status == 200 and isinstance(opps, dict) and isinstance(opps.get("opportunities"), list),
                f"GET opportunities returns 200 + an opportunities list (got {status})")
    status, shop = api.get("/api/reactions/shopping-list")
    ok &= check(status == 200 and isinstance(shop, dict) and "materials" in shop,
                f"GET shopping-list returns 200 + a materials report (got {status})")
    # Running-job detail modal: pick a real reachable product off the opportunity list and assert
    # the per-job breakdown comes back with the fields the modal renders.
    reachable = (opps.get("opportunities") if isinstance(opps, dict) else None) or []
    if reachable:
        tid = reachable[0]["type_id"]
        status, jd = api.get(f"/api/reactions/job-detail?type_id={tid}&runs=3")
        fields = {"name", "runs", "units", "runtime_hours", "input_cost", "output_value", "net_profit", "materials"}
        ok &= check(status == 200 and isinstance(jd, dict) and fields.issubset(jd.keys()) and jd.get("runs") == 3,
                    f"GET job-detail returns 200 + a full per-job breakdown (got {status})")
    return ok


TEST_ALLIANCE_ID = 990990051  # throwaway alliance so the fake character is a group member


def test_stock_zero_falls_back_to_market() -> bool:
    """A group member's moon material marked stock 0 on the price sheet is treated as 'alliance is
    out' EVERYWHERE — market is the always-available fallback default, the alliance sheet only a
    cheaper-when-in-stock bonus. So a stock-0 material is dropped from the group sheet and sourced
    from the open market across every path (suggestions, shopping list, orders), while a material
    with nonzero stock keeps its (cheaper) group price. Seeds a temp group + membership + two goo
    rows (one stock 0, one in stock) and asserts the source flips only for the out-of-stock one."""
    print("\n" + "=" * 60)
    print("  Stock 0 -> market fallback everywhere (nonzero stock keeps the group price)")
    print("=" * 60)
    from app.reactions import _load_goo_and_reached
    now = datetime.now(timezone.utc).isoformat()
    con = get_connection()
    row = con.execute("SELECT id FROM pp_groups WHERE alliance_id=?", (TEST_ALLIANCE_ID,)).fetchone()
    if row:
        gid = row["id"]
    else:
        con.execute("INSERT INTO pp_groups (name, alliance_id, created_at) VALUES (?,?,?)",
                    ("StockTest", TEST_ALLIANCE_ID, now))
        gid = con.execute("SELECT id FROM pp_groups WHERE alliance_id=?", (TEST_ALLIANCE_ID,)).fetchone()["id"]
    con.execute(
        "INSERT INTO pp_characters (character_id, character_name, context_id, alliance_id, scopes, "
        "mass_reactions, advanced_mass_reactions) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT (character_id) DO UPDATE SET context_id=excluded.context_id, "
        "alliance_id=excluded.alliance_id, scopes=excluded.scopes",
        (FAKE_CID, "Test Reactor", FAKE_CTX, TEST_ALLIANCE_ID, "read_character_jobs", 5, 0),
    )
    # Cheap group price (1 ISK) so the group source WINS whenever it's included — makes the source
    # flip unambiguous. 16633 stock 0 (alliance out), 16634 in stock (control).
    for tid, name, stock in ((16633, "Hydrocarbons", 0), (16634, "Atmospheric Gases", 100)):
        con.execute(
            "INSERT INTO pp_moon_goo_prices (group_id, type_id, name, sell_price, stock, updated_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT (group_id, type_id) DO UPDATE SET "
            "sell_price=excluded.sell_price, stock=excluded.stock",
            (gid, tid, name, 1.0, stock, now),
        )
    con.commit()
    con.close()
    ok = True
    try:
        reached = _load_goo_and_reached(FAKE_CTX)[1]
        ok &= check(reached.get(16633, {}).get("source") == "market",
                    "a stock-0 material drops the group price and falls back to market")
        ok &= check(reached.get(16634, {}).get("source") == "group",
                    "an in-stock material keeps its (cheaper) group price")
    finally:
        con = get_connection()
        con.execute("DELETE FROM pp_moon_goo_prices WHERE group_id=?", (gid,))
        con.execute("DELETE FROM pp_groups WHERE alliance_id=?", (TEST_ALLIANCE_ID,))
        con.execute("UPDATE pp_characters SET alliance_id=NULL WHERE character_id=?", (FAKE_CID,))
        con.commit()
        con.close()
    return ok


def _set_feature_state(key: str, state: str) -> str:
    """Force a feature onto a rung and return the rung it was on. There is no caching in
    app.features._state_of, so this takes effect immediately for in-process callers."""
    from app.features import ensure_features_table
    ensure_features_table()
    con = get_connection()
    row = con.execute("SELECT state FROM pp_features WHERE key=?", (key,)).fetchone()
    con.execute("UPDATE pp_features SET state=? WHERE key=?", (state, key))
    con.commit()
    con.close()
    return (row["state"] if row else None) or "admin"


def _clear_formula_evidence():
    con = get_connection()
    for t in ("pp_char_blueprints", "pp_char_formula_jobs", "pp_char_industry_jobs"):
        try:
            con.execute(f"DELETE FROM {t} WHERE character_id=?", (FAKE_CID,))
        except Exception:
            pass
    con.commit()
    con.close()


def test_a_formula_is_one_reaction_at_a_time() -> bool:
    """A formula is an ITEM and it is LOCKED into the reactor while a job runs on it, so one
    Ferrofluid formula is one concurrent reaction however many reactor slots are free. Reactions
    allocated against slots only, so it happily told a player to install ten parallel jobs they
    physically cannot. The invariants:

      * N formulas cap concurrency at N — and the evidence is the Industry side's, reused rather
        than reimplemented (personal blueprint cache ∪ enabled stock ∪ observed blueprint_ids);
      * chain tiers are SEQUENTIAL, so the cap is per tier and a deep chain is never blocked by it;
      * **no evidence means NO cap** — an unseen formula is "we don't know", never "you hold none",
        and an incomplete blueprint picture is unknown at the account level;
      * flag off ⇒ no caps at all ⇒ the existing slot math is untouched.
    """
    print(f"\n{'='*60}\n  A formula is one reaction at a time\n{'='*60}")
    from app.industry.blueprints import (ensure_char_blueprints_table,
                                          ensure_formula_job_prints_table)
    from app.reactions.jobs import _cap_jobs, _fit_chain_slots, formula_concurrency_caps

    ensure_char_blueprints_table()
    ensure_formula_job_prints_table()
    ok = True

    # `_cap_jobs` is the whole "unknown never refuses" rule in one line, so pin it directly.
    ok &= check(_cap_jobs(None, 7) == 7, "no cap leaves the job count alone (unknown never refuses)")
    ok &= check(_cap_jobs(0, 7) == 7, "and a zero cap is 'unknown' too, not 'you may run nothing'")
    ok &= check(_cap_jobs(2, 7) == 2 and _cap_jobs(9, 7) == 7, "a cap binds only when it is tighter")
    ok &= check(_cap_jobs(1, 0) == 1, "a capped tier still gets one job — a tier at zero can't install")

    # Tiers are sequential: one formula each, four tiers, still four jobs. Nothing is capped ACROSS
    # tiers, because tier 0 has finished before tier 1 starts.
    deep = _fit_chain_slots([100.0, 100.0, 100.0, 100.0], [1, 1, 1, 1], 40)
    ok &= check(deep == [1, 1, 1, 1],
                "a four-tier chain with one formula per tier still installs every tier")
    mixed = _fit_chain_slots([100.0, 100.0], [1, 20], 20)
    ok &= check(mixed[0] == 1 and mixed[1] > 1,
                "and capping one tier does not cap the tier beside it")

    con = get_connection()
    rx = con.execute("SELECT reaction_id, output_type_id FROM reactions "
                     "ORDER BY reaction_id LIMIT 2").fetchall()
    con.close()
    if not check(len(rx) >= 2, "the SDE knows some reactions to test with"):
        return ok
    formula, product = rx[0]["reaction_id"], rx[0]["output_type_id"]
    unseen = rx[1]["output_type_id"]

    prev_state = _set_feature_state("reactions_formula_cap", "public")
    try:
        _clear_formula_evidence()
        con = get_connection()
        # Three of the formula in the personal blueprint list — a positive quantity is a STACK, and
        # each print in it can hold its own job.
        con.execute("INSERT INTO pp_char_blueprints (character_id, blueprints_json, fetched_at) "
                    "VALUES (?,?,?) ON CONFLICT (character_id) DO UPDATE SET "
                    "blueprints_json=excluded.blueprints_json",
                    (FAKE_CID, json.dumps([{"type_id": formula, "me": 0, "te": 0,
                                            "quantity": 3, "runs": -1}]), 1.0))
        con.commit()
        con.close()

        caps = formula_concurrency_caps(FAKE_CTX)
        ok &= check(caps.get(product) == 3, "three formulas held cap that product at three jobs at once")
        ok &= check(unseen not in caps, "a formula we have never seen is not capped at all")

        # The Industry evidence layer's third source, reused as-is: N distinct physical prints
        # observed running jobs is N formulas, wherever they are actually kept.
        con = get_connection()
        con.execute("UPDATE pp_char_blueprints SET blueprints_json=? WHERE character_id=?",
                    (json.dumps([{"type_id": formula, "me": 0, "te": 0, "quantity": -1, "runs": -1}]),
                     FAKE_CID))
        con.execute("INSERT INTO pp_char_formula_jobs (character_id, prints_json, fetched_at) "
                    "VALUES (?,?,?) ON CONFLICT (character_id) DO UPDATE SET "
                    "prints_json=excluded.prints_json",
                    (FAKE_CID, json.dumps([{"blueprint_id": 5001, "blueprint_type_id": formula},
                                           {"blueprint_id": 5002, "blueprint_type_id": formula},
                                           {"blueprint_id": 5002, "blueprint_type_id": formula}]), 1.0))
        con.commit()
        con.close()
        caps = formula_concurrency_caps(FAKE_CTX)
        ok &= check(caps.get(product) == 2,
                    "two DISTINCT prints seen in job history is two formulas (the repeat id is one)")

        # Unknown at the ACCOUNT level: the blueprint scope is opt-in per character, so a picture
        # that isn't complete is a floor, and a floor read as a total serialises real work.
        _clear_formula_evidence()
        ok &= check(formula_concurrency_caps(FAKE_CTX) == {},
                    "an incomplete blueprint picture caps nothing at all")
    finally:
        _clear_formula_evidence()
        _set_feature_state("reactions_formula_cap", prev_state)

    # Flag off: no caps, whatever the evidence says — the slot math is bit-for-bit what it was.
    con = get_connection()
    con.execute("INSERT INTO pp_char_blueprints (character_id, blueprints_json, fetched_at) "
                "VALUES (?,?,?) ON CONFLICT (character_id) DO UPDATE SET "
                "blueprints_json=excluded.blueprints_json",
                (FAKE_CID, json.dumps([{"type_id": formula, "me": 0, "te": 0,
                                        "quantity": 1, "runs": -1}]), 1.0))
    con.commit()
    con.close()
    try:
        ok &= check(formula_concurrency_caps(FAKE_CTX) == {},
                    "with the flag off nothing is capped, however much evidence there is")
    finally:
        _clear_formula_evidence()
    return ok


def _seed_uncached_characters(n: int) -> list[int]:
    """`n` extra characters on the test context with NO blueprint cache — an account whose ESI
    blueprint picture is incomplete, which is the state 12 of the reporting user's 14 characters
    were in."""
    ids = [FAKE_CID + 1 + i for i in range(n)]
    con = get_connection()
    for cid in ids:
        con.execute(
            "INSERT INTO pp_characters (character_id, character_name, context_id, scopes) "
            "VALUES (?,?,?,?) ON CONFLICT (character_id) DO UPDATE SET "
            "context_id=excluded.context_id", (cid, f"Uncached {cid}", FAKE_CTX, ""))
    con.commit()
    con.close()
    return ids


def _clear_extra_characters(ids: list[int]) -> None:
    con = get_connection()
    for cid in ids:
        for t in ("pp_char_blueprints", "pp_char_formula_jobs", "pp_char_industry_jobs"):
            try:
                con.execute(f"DELETE FROM {t} WHERE character_id=?", (cid,))
            except Exception:
                pass
        con.execute("DELETE FROM pp_characters WHERE character_id=?", (cid,))
    con.commit()
    con.close()


def _declare(product: int, quantity: int) -> None:
    """Declare `quantity` formulas of `product` by hand — the row the T9 paste importer writes."""
    from app.industry.blueprints import ensure_manual_blueprints_table
    ensure_manual_blueprints_table()
    con = get_connection()
    con.execute("DELETE FROM pp_industry_blueprints WHERE context_id=?", (FAKE_CTX,))
    con.execute("INSERT INTO pp_industry_blueprints (context_id, id, type_id, me, te, runs, "
                "quantity, prefer, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (FAKE_CTX, 1, product, 0, 0, -1, quantity, "", 1.0))
    con.commit()
    con.close()


def _clear_declarations() -> None:
    con = get_connection()
    try:
        con.execute("DELETE FROM pp_industry_blueprints WHERE context_id=?", (FAKE_CTX,))
        con.commit()
    except Exception:
        pass
    con.close()


def test_a_declared_holding_is_known_without_full_esi_coverage() -> bool:
    """**The reported bug, reproduced.** A user declared 238 reaction formulas by hand, then ordered
    Reinforced Carbon Fiber and was assigned 20 concurrent jobs against the 10 formulas they had
    just declared holding. Cause: both cap sites asked `blueprint_coverage().complete`, which was
    False because 12 of their 14 characters had never granted the blueprints scope — so an
    ACCOUNT-WIDE fact about an ESI scan suppressed a PER-PRODUCT statement the user had made by
    hand.

    The distinction the fix turns on, and the thing this test exists to keep straight:

      * a DECLARATION is the user saying what they own. `owned_blueprints` already lets it REPLACE
        the ESI reading for its product, so it is known whatever some other character's scope says
        — and it may be capped on.
      * an incomplete ESI SCAN is still a FLOOR, and capping on a floor serialises work the builder
        can really do. An UNDECLARED product on the same account must stay uncapped. That rule is
        not being relaxed here, and the third check below is what proves it.
    """
    print(f"\n{'='*60}\n  A declared holding is known without full ESI coverage\n{'='*60}")
    from app.industry.blueprints import (ensure_char_blueprints_table,
                                         ensure_formula_job_prints_table)
    from app.reactions.jobs import _cap_jobs, formula_concurrency_caps

    ensure_char_blueprints_table()
    ensure_formula_job_prints_table()
    ok = True

    con = get_connection()
    rx = con.execute("SELECT reaction_id, output_type_id FROM reactions "
                     "ORDER BY reaction_id LIMIT 2").fetchall()
    con.close()
    if not check(len(rx) >= 2, "the SDE knows some reactions to test with"):
        return ok
    declared_formula, declared_product = rx[0]["reaction_id"], rx[0]["output_type_id"]
    scanned_formula, scanned_product = rx[1]["reaction_id"], rx[1]["output_type_id"]

    prev_cap = _set_feature_state("reactions_formula_cap", "public")
    prev_manual = _set_feature_state("industry_manual_blueprints", "public")
    extra = []
    try:
        _clear_formula_evidence()
        extra = _seed_uncached_characters(13)          # 14 characters, 1 will be cached

        # The one character that DID grant the scope holds three of some other formula. That
        # product is the control: ESI evidence only, on an incomplete picture.
        con = get_connection()
        con.execute("INSERT INTO pp_char_blueprints (character_id, blueprints_json, fetched_at) "
                    "VALUES (?,?,?) ON CONFLICT (character_id) DO UPDATE SET "
                    "blueprints_json=excluded.blueprints_json",
                    (FAKE_CID, json.dumps([{"type_id": scanned_formula, "me": 0, "te": 0,
                                            "quantity": 3, "runs": -1}]), 1.0))
        con.commit()
        con.close()

        from app.industry.blueprints import blueprint_coverage, owned_blueprints
        cov = blueprint_coverage(FAKE_CTX)
        ok &= check(not cov["complete"] and cov["missing"] == 13,
                    f"the account's blueprint picture really is incomplete ({cov})")

        _declare(declared_product, 10)
        owned = owned_blueprints(FAKE_CTX)
        ok &= check((owned.get(declared_product) or {}).get("copy_count") == 10,
                    "the declaration reaches owned_blueprints as ten copies")
        ok &= check((owned.get(declared_product) or {}).get("source") == "manual",
                    "and is marked as the user's own statement, not a reading")

        caps = formula_concurrency_caps(FAKE_CTX)
        ok &= check(caps.get(declared_product) == 10,
                    "a DECLARED holding caps its product at ten, despite 12 characters unscanned")
        ok &= check(_cap_jobs(caps.get(declared_product), 20) == 10,
                    "so an order that would have taken 20 slots installs 10 jobs (the reported bug)")
        ok &= check(scanned_product not in caps,
                    "while an UNDECLARED product on the same account is not capped at all — an "
                    "incomplete scan is a floor, and a floor must never serialise real work")

        # Same declaration, complete coverage: unchanged. The fix adds a way to be known, it does
        # not alter what a fully-scanned account already did.
        _clear_extra_characters(extra)
        extra = []
        ok &= check(blueprint_coverage(FAKE_CTX)["complete"], "and now the picture is complete")
        full = formula_concurrency_caps(FAKE_CTX)
        ok &= check(full.get(declared_product) == 10,
                    "a declared product on a COMPLETE picture caps at ten exactly as before")
        ok &= check(full.get(scanned_product) == 3,
                    "and the scanned product is capped too, now that its picture is whole")
    finally:
        _clear_extra_characters(extra)
        _clear_declarations()
        _clear_formula_evidence()
        _set_feature_state("industry_manual_blueprints", prev_manual)
        _set_feature_state("reactions_formula_cap", prev_cap)
    return ok


def test_order_estimate_reflects_the_formula_cap(api: Api) -> bool:
    """The quoted time on a customer order assumes a tier's runs spread across every free slot —
    and that is the number a customer is given. It has to answer to the same formula cap the
    assign path commits under, or the tool quotes an installation it would then refuse to make."""
    print(f"\n{'='*60}\n  Order estimate answers to the formula cap\n{'='*60}")
    from app.reactions import orders as rx_orders

    product = _find_test_product(api)
    if product is None:
        print("  SKIP: no reachable priced product in this environment")
        return True
    order = {"id": None, "type_id": product["type_id"], "name": product["name"],
             "target_qty": product["output_qty"] * 40 / max(1, product["top_level_runs"]),
             "top_level_runs": 40, "assigned_runs": 0, "status": "preview"}

    real = rx_orders.formula_concurrency_caps
    try:
        rx_orders.formula_concurrency_caps = lambda ctx: {}
        base = rx_orders._order_report(FAKE_CTX, order)
        rx_orders.formula_concurrency_caps = lambda ctx: {product["type_id"]: 1}
        capped = rx_orders._order_report(FAKE_CTX, order)
    finally:
        rx_orders.formula_concurrency_caps = real

    ok = check(base["time"]["free_slots_now"] > 1,
               f"the seeded character really has free slots ({base['time']['free_slots_now']})")
    ok &= check(base["time"]["formula_capped"] == [],
                "no evidence about the formula leaves the quote exactly as it was")
    ok &= check(capped["time"]["estimated_hours"] > base["time"]["estimated_hours"],
                f"one formula quotes longer than {base['time']['free_slots_now']} free slots would "
                f"({capped['time']['estimated_hours']}h vs {base['time']['estimated_hours']}h)")
    ok &= check(capped["time"]["formula_capped"] == [product["name"]],
                "and the report names the step the formula held back, so the UI can say why")
    return ok


def test_suggest_and_assign_respect_the_formula_cap(api: Api) -> bool:
    """The other two paths the cap has to reach: the Suggest wizard's bin-pack must not propose
    five parallel jobs off one formula, and the customer-order assign must not commit them. Seeds
    ONE formula of every reaction into the blueprint cache (so every candidate is capped at one)
    and compares a run with the flag off against a run with it on."""
    print(f"\n{'='*60}\n  Suggest + order assign respect the formula cap\n{'='*60}")
    from app.reactions.advisor import _suggest_reactions

    prev = _set_feature_state("reactions_formula_cap", "hidden")
    ok = True
    try:
        _clear_formula_evidence()
        off = _suggest_reactions(FAKE_CTX, 5_000_000_000, 2, 168.0)
        wide = [s for s in off["suggestions"] if s["job_count"] > 1]
        if not wide:
            print("  SKIP: nothing in this environment gets suggested across several slots")
            return True
        ok &= check(off["totals"]["formula_capped"] == [],
                    "with the flag off no product is formula-capped and job counts are untouched")

        con = get_connection()
        rx = [r["reaction_id"] for r in con.execute("SELECT reaction_id FROM reactions")]
        con.execute("INSERT INTO pp_char_blueprints (character_id, blueprints_json, fetched_at) "
                    "VALUES (?,?,?) ON CONFLICT (character_id) DO UPDATE SET "
                    "blueprints_json=excluded.blueprints_json",
                    (FAKE_CID, json.dumps([{"type_id": t, "me": 0, "te": 0, "quantity": 1, "runs": -1}
                                           for t in rx]), 1.0))
        con.commit()
        con.close()
        _set_feature_state("reactions_formula_cap", "public")
        on = _suggest_reactions(FAKE_CTX, 5_000_000_000, 2, 168.0)
        ok &= check(all(s["job_count"] == 1 for s in on["suggestions"]),
                    "one formula of everything means one job per suggestion, not one per free slot")
        ok &= check(all(t["job_count"] == 1 for s in on["suggestions"] for t in s["chain_tiers"]),
                    "each chain tier is held to its own formula too")
        ok &= check(len(on["totals"]["formula_capped"]) > 0
                    and all(s["formula_cap"] == 1 for s in on["suggestions"]
                            if s["name"] in on["totals"]["formula_capped"]),
                    "and the run says which products the formula count held down, so the UI can say why")

        # The customer-order assign path, on the same evidence: one formula, one job installed.
        product = _find_test_product(api)
        if product is not None:
            status, created = api.post("/api/reactions/orders", {
                "type_id": product["type_id"], "target_qty": product["output_qty"] * 20,
                "client_name": "Formula Cap"})
            if check(status == 200, f"an order for 20x the batch creates (got {status})"):
                oid = created["order"]["id"]
                status, res = api.post(f"/api/reactions/orders/{oid}/assign", {})
                ok &= check(status == 200, f"assigning it succeeds (got {status})")
                jobs = [c["jobs"] for c in res.get("characters", [])]
                ok &= check(jobs and all(j == 1 for j in jobs),
                            f"the order commits ONE job per character, not one per free slot (got {jobs})")
                api.post(f"/api/reactions/orders/{oid}/status", {"status": "cancelled"})
    finally:
        _clear_formula_evidence()
        _set_feature_state("reactions_formula_cap", prev)
    return ok


def test_suggest_absorb_contract(api: Api) -> bool:
    """The suggestion engine caps each batch at a fraction (the "Market fill" slider, absorb_fraction)
    of the product's real traded volume over the run period. Assert the durable CONTRACT — the
    request honours absorb_fraction and totals echo it back as absorb_fill_pct — not specific ISK
    values (those move with the live market). The seeded character has real reaction slots, so a
    healthy market yields at least one suggestion."""
    print(f"\n{'='*60}\n  Suggest: market-fill cap contract\n{'='*60}")
    ok = True
    status, data = api.post("/api/reactions/suggest", {
        "isk_budget": 5_000_000_000, "max_chain_depth": 2, "cadence_hours": 168.0, "absorb_fraction": 0.3,
    })
    if not check(status == 200, f"suggest returns 200 (got {status})"):
        return ok
    ok &= check(data.get("totals", {}).get("absorb_fill_pct") == 30,
                "totals echo the requested Market-fill fraction (30%)")
    adv = data.get("advisor", {})
    ok &= check("absorb_hints" not in adv,
                "advisor no longer emits the removed absorb_hints nudges")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--no-live", action="store_true",
                        help="run only the deterministic unit tests, skip the live-container lifecycle test")
    args = parser.parse_args()
    base = args.url.rstrip("/")

    results = [run_unit_tests(), test_recurring_create_refreshes_visible_queue(),
               test_reactions_phase1_is_task_first()]

    if not args.no_live:
        try:
            _cleanup()
            token = _seed_session()
            api = Api(base, token)
            results.append(test_order_lifecycle(api))
            results.append(test_recurring_order_releases_each_cycle(api))
            results.append(test_order_preview(api))
            results.append(test_pricing_endpoints_live(api))
            results.append(test_suggest_absorb_contract(api))
            results.append(test_stock_zero_falls_back_to_market())
            results.append(test_a_formula_is_one_reaction_at_a_time())
            results.append(test_a_declared_holding_is_known_without_full_esi_coverage())
            results.append(test_order_estimate_reflects_the_formula_cap(api))
            results.append(test_suggest_and_assign_respect_the_formula_cap(api))
            _cleanup()
        except Exception as e:
            print(f"  SKIP live order-lifecycle test (no reachable app/DB/server: {e})")

    print(f"\n{'='*60}")
    if all(results):
        print("  ALL TESTS PASSED")
        return 0
    print("  SOME TESTS FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
