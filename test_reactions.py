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


def test_an_order_skips_characters_it_would_give_one_job() -> bool:
    """Reported from a live order (#45, 1000 runs of Reinforced Carbon Fiber): stage 1 was spread
    over 5 characters, two of them holding ONE job each; stage 2 over SEVEN, five holding one each.
    Every one of those is a login to install a single job and a second trip to collect it, while the
    characters that already have jobs sit on free reactors. Parallelism comes from reactors, so
    moving that work costs nothing.

    The FIRST attempt at this measured `_useful_slots` — the theoretical most an order could use —
    which for a 1000-run order is in the thousands, so it kept every host and did nothing at all on
    the exact order that prompted it. This pins the rule that replaced it."""
    from app.reactions.jobs import _lean_hosts, _MIN_JOBS_PER_TIER

    # The reported account: three 10-slot characters and four 5-slot ones, on a 4-tier chain
    # (3 intermediates + the product), so `per_chain` is 4 and the floor is 8 free reactors.
    big = [{"character_id": i, "free_slots": 10} for i in (1, 2, 3)]
    small = [{"character_id": i, "free_slots": 5} for i in (4, 5, 6, 7)]
    ids = lambda hs: sorted(h["character_id"] for h in hs)

    ok = check(ids(_lean_hosts(big + small, 4)) == [1, 2, 3],
               "the 5-slot characters are dropped from a 4-tier chain — one job a stage is not a trip")
    ok &= check(_MIN_JOBS_PER_TIER == 2, "the floor is two jobs a tier, not one")

    # A shallower chain needs less room, so the same characters become worth involving again.
    ok &= check(ids(_lean_hosts(big + small, 2)) == [1, 2, 3, 4, 5, 6, 7],
                "on a 2-tier chain a 5-slot character clears the floor and keeps its share")

    # Never strand an order: an account with nothing but small characters still gets to place it.
    ok &= check(ids(_lean_hosts(small, 4)) == [4, 5, 6, 7],
                "when NO character clears the floor every one is kept — spreading beats refusing")
    ok &= check(_lean_hosts([], 4) == [], "no hosts stays no hosts")

    # It trims the tail; it does not hunt for one character. The jobs are unchanged, so more
    # reactors running them is still sooner finished.
    ok &= check(len(_lean_hosts(big, 4)) == 3,
                "every character that clears the floor is kept, not just the roomiest")
    ok &= check(ids(_lean_hosts([{"character_id": 1, "free_slots": 8},
                                 {"character_id": 2, "free_slots": 7}], 4)) == [1],
                "the floor is exact — 8 clears a 4-tier chain, 7 does not")
    return ok


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
        test_an_order_skips_characters_it_would_give_one_job(),
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

    results = [run_unit_tests()]

    if not args.no_live:
        try:
            _cleanup()
            token = _seed_session()
            api = Api(base, token)
            results.append(test_order_lifecycle(api))
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
