"""
Moon-goo reaction profitability ranking (Phase 2 of the B0SS reactions tool — see
app/moon_goo.py for the alliance price list this reads from). Not part of the PI planner's
extractor/factory distribution algorithm — a separate, unrelated read-only advisory tool.

Starting from whatever goo is actually in stock (app.moon_goo's pp_moon_goo_prices), walks the
reaction graph forward (Simple -> Composite, any depth) to find every reachable product, and
for each computes: cost to make the max achievable quantity (capped by stock, ME-adjusted),
value at Jita (both instant-sell/buy and sell-order/ask, with order-book depth alongside so the
caller can judge liquidity), and shipping+collateral cost to get it there. Ranks by profit but
returns every dimension (steps, profit/m3, volume) un-collapsed — "advice, not a tool": the
comparison happens client-side, this doesn't pick a single winner.

This evaluates each candidate chain IN ISOLATION (as if all available goo went to that one
product) — it does not account for competing chains sharing the same raw materials. That
cross-product allocation is Phase 3's job (an LP over reaction-slot + stock constraints).
"""
import json as _json
import time as _time
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.sde import get_connection, load_pi_data, ensure_once
from app.market import fetch_market_data
from app.esi import require_b0ss, require_admin, require_context, ESI_BASE, _get_valid_token

router = APIRouter()

# Standup L-Set Reactor Efficiency I (T1) — the rig actually fitted, confirmed via EVE Ref.
# -2% material / -20% time base, x1.1 in null/WH space. Only the material figure matters here.
REACTION_ME_REDUCTION = 0.022

# Shared, admin-configurable shipping/collateral rates — these are alliance-wide assumptions
# (courier rates, insurance terms) that change over time, not a per-user preference, so this
# is a single global row (no context_id) rather than per-account settings like alert_settings.
# Import (buying materials in from Jita) has no collateral — that's a self-haul/no-3rd-party-
# courier assumption; only the export leg (shipping the reacted product OUT to sell) uses a
# courier and needs collateral declared. Confirmed with the user: moon goo itself has zero
# import cost (picked up at/near the reaction site), only non-goo purchased inputs (fuel
# blocks etc.) pay the import rate.
_RXS_DEFAULTS = {"import_isk_per_m3": 1200.0, "export_isk_per_m3": 1200.0, "export_collateral_pct": 0.005}


@ensure_once
def ensure_reaction_settings_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_reaction_settings (
                id                   INTEGER PRIMARY KEY,
                import_isk_per_m3    REAL NOT NULL DEFAULT 1200,
                export_isk_per_m3    REAL NOT NULL DEFAULT 1200,
                export_collateral_pct REAL NOT NULL DEFAULT 0.005
            )
        """)
        con.commit()
    finally:
        con.close()


def get_reaction_settings() -> dict:
    """Effective shipping/collateral settings — the saved singleton row if an admin has
    customized it, else the defaults above. Nothing changes in behavior until an admin edits
    these (same convention as app.alert_settings)."""
    ensure_reaction_settings_table()
    con = get_connection()
    try:
        row = con.execute(
            "SELECT import_isk_per_m3, export_isk_per_m3, export_collateral_pct "
            "FROM pp_reaction_settings WHERE id=1"
        ).fetchone()
    finally:
        con.close()
    if not row:
        return dict(_RXS_DEFAULTS)
    return {
        "import_isk_per_m3": row["import_isk_per_m3"],
        "export_isk_per_m3": row["export_isk_per_m3"],
        "export_collateral_pct": row["export_collateral_pct"],
    }


class ReactionSettingsUpdate(BaseModel):
    import_isk_per_m3: float
    export_isk_per_m3: float
    export_collateral_pct: float


@router.get("/api/reactions/settings")
def api_get_reaction_settings(ctx: int = Depends(require_b0ss)):
    return get_reaction_settings()


@router.put("/api/reactions/settings")
def api_update_reaction_settings(req: ReactionSettingsUpdate, ctx: int = Depends(require_admin)):
    ensure_reaction_settings_table()
    con = get_connection()
    try:
        con.execute(
            "INSERT INTO pp_reaction_settings (id, import_isk_per_m3, export_isk_per_m3, export_collateral_pct) "
            "VALUES (1, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET "
            "import_isk_per_m3=excluded.import_isk_per_m3, export_isk_per_m3=excluded.export_isk_per_m3, "
            "export_collateral_pct=excluded.export_collateral_pct",
            (req.import_isk_per_m3, req.export_isk_per_m3, req.export_collateral_pct),
        )
        con.commit()
    finally:
        con.close()
    return get_reaction_settings()


def _load_reaction_graph(con) -> tuple[dict[int, list[dict]], dict[int, list[dict]]]:
    """Returns (reactions_by_output, inputs_by_reaction): reactions_by_output maps a product
    type_id to the list of reaction formulas that can produce it (usually 1, occasionally 2 —
    e.g. a small no-fuel batch vs. a large fuel-block-consuming batch of the same conversion;
    both are kept as separate candidate paths rather than picking one). Each formula dict is
    {reaction_id, output_qty, cycle_time, inputs: [{type_id, quantity}]}."""
    inputs_by_reaction: dict[int, list[dict]] = {}
    for r in con.execute("SELECT reaction_id, type_id, quantity FROM reaction_inputs"):
        inputs_by_reaction.setdefault(r["reaction_id"], []).append(
            {"type_id": r["type_id"], "quantity": r["quantity"]})

    reactions_by_output: dict[int, list[dict]] = {}
    for r in con.execute("SELECT reaction_id, output_type_id, output_qty, cycle_time FROM reactions"):
        formula = {
            "reaction_id": r["reaction_id"], "output_qty": r["output_qty"],
            "cycle_time": r["cycle_time"], "inputs": inputs_by_reaction.get(r["reaction_id"], []),
        }
        reactions_by_output.setdefault(r["output_type_id"], []).append(formula)
    return reactions_by_output, inputs_by_reaction


def _resolve_reachable(goo: dict[int, dict], purchasable: dict[int, float],
                        reactions_by_output: dict[int, list[dict]]) -> dict[int, dict]:
    """Fixed-point expansion from the available goo (type_id -> {sell_price, stock}) through
    the reaction graph. Returns {type_id: node} for every reachable node (goo, market-bought
    inputs, AND every reaction product reachable at any depth), where node carries:
      unit_cost      - ISK to produce one unit, rolled down to raw goo/market cost (ME-adjusted)
      max_qty        - max units producible given available stock through the WHOLE chain
      reaction_count - distinct reaction runs needed in the subtree (the "work" proxy)
      via            - None for raw goo/purchasable, else the {reaction_id, ...} formula used
    A reaction only becomes reachable once every one of its inputs is already reachable —
    same "expand until no more nodes unlock" shape as build_sde.py's compute_pi_tiers, just
    walked forward from available inputs instead of backward from a fixed target.

    `purchasable` seeds every reaction input that ISN'T alliance moon goo (fuel blocks, and any
    other named material most reaction formulas need alongside the moon materials) at its Jita
    buy price with effectively unlimited supply — these trade in bulk on the open market, they
    aren't a stock-limited resource the way the alliance's goo deal is. Without this, almost no
    reaction is ever reachable at all (confirmed live: every Composite/Hybrid formula and most
    Simple ones need at least one fuel block)."""
    reached: dict[int, dict] = {
        tid: {"unit_cost": g["sell_price"], "max_qty": g["stock"], "reaction_count": 0, "via": None}
        for tid, g in goo.items() if g["stock"] > 0
    }
    for tid, buy_price in purchasable.items():
        if tid not in reached and buy_price > 0:
            reached[tid] = {"unit_cost": buy_price, "max_qty": float("inf"), "reaction_count": 0, "via": None}

    changed = True
    while changed:
        changed = False
        for output_id, formulas in reactions_by_output.items():
            best = None
            for f in formulas:
                if not f["inputs"] or any(inp["type_id"] not in reached for inp in f["inputs"]):
                    continue
                # A chain built ONLY from purchasable (unlimited) inputs never actually touches
                # real moon goo — not a genuine "goo reaction" opportunity, and would divide by
                # an infinite max_qty below. Require at least one finite (goo-backed) input.
                if all(reached[inp["type_id"]]["max_qty"] == float("inf") for inp in f["inputs"]):
                    continue
                # ME reduces material CONSUMED per run, so it doesn't change unit_cost's
                # normalization directly — it scales down how much of each input a run needs.
                eff_qty = {inp["type_id"]: inp["quantity"] * (1 - REACTION_ME_REDUCTION)
                           for inp in f["inputs"]}
                runs = min(reached[tid]["max_qty"] / q for tid, q in eff_qty.items())
                if runs <= 0 or runs == float("inf"):
                    continue
                cost_per_run = sum(q * reached[tid]["unit_cost"] for tid, q in eff_qty.items())
                reaction_count = 1 + sum(reached[inp["type_id"]]["reaction_count"] for inp in f["inputs"])
                candidate = {
                    "unit_cost": cost_per_run / f["output_qty"],
                    "max_qty": int(runs) * f["output_qty"],
                    "reaction_count": reaction_count,
                    # Actual reaction-job cycles of THIS specific formula needed to hit max_qty
                    # (distinct from reaction_count, which is chain DEPTH/distinct-formula count,
                    # not run count) — this is what the wizard's "steps budget" (confirmed by the
                    # user: total reaction runs, not chain complexity) actually constrains.
                    # Deliberately counts only the top-level formula's own cycles, not upstream
                    # feeder reactions' cycles too — a documented simplification, not a full
                    # multi-level rollup (see Phase 3c plan notes).
                    "top_level_runs": int(runs),
                    "cycle_time": f["cycle_time"],
                    "via": {"reaction_id": f["reaction_id"], "cycle_time": f["cycle_time"],
                            "inputs": f["inputs"]},
                }
                # Prefer whichever formula yields lower unit_cost (most profitable path to
                # this product) — "least work most profitable" means the tool should already
                # have picked the cheap recipe, not surface a worse one alongside it.
                if best is None or candidate["unit_cost"] < best["unit_cost"]:
                    best = candidate
            if best is not None and (output_id not in reached or best["unit_cost"] < reached[output_id]["unit_cost"]):
                reached[output_id] = best
                changed = True

    return reached


def _build_opportunities(context_id: int) -> list[dict]:
    # try/finally is load-bearing: an exception between get_connection() and close() (e.g. the
    # reactions/reaction_inputs tables not existing yet on a freshly-deployed environment) would
    # otherwise leak the connection permanently out of the small per-pod pool.
    con = get_connection()
    try:
        goo_rows = con.execute("SELECT type_id, sell_price, stock FROM pp_moon_goo_prices").fetchall()
        goo = {r["type_id"]: {"sell_price": r["sell_price"], "stock": r["stock"]} for r in goo_rows}
        reactions_by_output, inputs_by_reaction = _load_reaction_graph(con)
    finally:
        con.close()

    if not goo:
        return []

    settings = get_reaction_settings()
    pi = load_pi_data()
    types = pi["types"]

    # Every reaction input that isn't alliance moon goo (fuel blocks, and other named
    # materials most formulas need alongside them) — priced at what it costs to instantly
    # ACQUIRE them (buy from existing Jita sell orders = the order book's sell_price; the
    # market_data field names are from the order book's perspective, not ours — buying costs
    # the sell price, selling earns the buy price) PLUS the configured import shipping cost
    # to get them from Jita to the reaction site (no collateral on the import leg — that's a
    # self-haul assumption, only the export leg uses a courier). Unlimited supply assumed.
    # Moon goo itself gets no import cost added (confirmed: already at/near the reaction site).
    all_input_ids = {inp["type_id"] for inputs in inputs_by_reaction.values() for inp in inputs}
    purchasable_ids = [tid for tid in all_input_ids if tid not in goo]
    purchasable_market = fetch_market_data(purchasable_ids)
    purchasable = {
        tid: m["sell_price"] + settings["import_isk_per_m3"] * (types.get(tid, {}).get("volume") or 0.0)
        for tid, m in purchasable_market.items()
    }

    reached = _resolve_reachable(goo, purchasable, reactions_by_output)
    # Only reaction PRODUCTS are candidates — shipping raw unreacted goo isn't what this tool
    # is for (that's just the input side).
    candidate_ids = [tid for tid, node in reached.items() if node["reaction_count"] > 0 and node["max_qty"] > 0]
    if not candidate_ids:
        return []

    market = fetch_market_data(candidate_ids)

    opportunities = []
    for tid in candidate_ids:
        node = reached[tid]
        type_info = types.get(tid, {})
        vol = type_info.get("volume") or 0.0
        m = market.get(tid)
        if not m:
            continue  # no live market data for this product — can't price it, skip rather than guess

        qty = node["max_qty"]
        input_cost = qty * node["unit_cost"]
        ship_volume = qty * vol
        shipping_cost = ship_volume * settings["export_isk_per_m3"]
        # Collateral is a transport cost (courier contract), charged regardless of how the
        # cargo is later sold — declared consistently against Jita sell (the standard
        # freight-collateral reference value), not whichever sell method ends up chosen.
        collateral_cost = qty * m["sell_price"] * settings["export_collateral_pct"]
        instant_value = qty * m["buy_price"]
        order_value = qty * m["sell_price"]
        fixed_costs = input_cost + shipping_cost + collateral_cost

        opportunities.append({
            "type_id": tid,
            "name": type_info.get("name", str(tid)),
            "steps": node["reaction_count"],
            "top_level_runs": node["top_level_runs"],
            "cycle_time": node["cycle_time"],
            "output_qty": qty,
            "input_cost": round(input_cost, 2),
            "shipping_volume_m3": round(ship_volume, 2),
            "shipping_cost": round(shipping_cost, 2),
            "collateral_cost": round(collateral_cost, 2),
            "instant_sell_value": round(instant_value, 2),
            "sell_order_value": round(order_value, 2),
            "net_profit_instant": round(instant_value - fixed_costs, 2),
            "net_profit_order": round(order_value - fixed_costs, 2),
            "profit_per_m3_instant": round((instant_value - fixed_costs) / ship_volume, 2) if ship_volume > 0 else None,
            "buy_volume": m["buy_volume"],
            "sell_volume": m["sell_volume"],
        })

    opportunities.sort(key=lambda o: -o["net_profit_instant"])
    return opportunities


@router.get("/api/reactions/opportunities")
def reactions_opportunities(context_id: int = Depends(require_b0ss)):
    return {"opportunities": _build_opportunities(context_id)}


# ── Personal reaction-job tracking (opt-in scope, see app.esi.INDUSTRY_JOBS_SCOPES) ────────────
# Cache-at-fetch, not live-fetch-on-every-page-load (same shape as app.pi_sim's colony state):
# ESI already reports start_date/end_date directly for a job, so there's no decay/rate math to
# simulate forward — just cache the raw filtered job list with a fetched_at timestamp, refreshed
# on demand (a "Refresh" button, same UX as the existing planet rescan) rather than polling.

@ensure_once
def ensure_industry_jobs_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_char_industry_jobs (
                character_id INTEGER PRIMARY KEY,
                jobs_json    TEXT NOT NULL DEFAULT '[]',
                fetched_at   REAL
            )
        """)
        con.commit()
    finally:
        con.close()


_structure_name_cache: dict[int, str] = {}  # structure names don't change — cache for process lifetime


def _resolve_structure_name(structure_id: int, access_token: str) -> str:
    if structure_id in _structure_name_cache:
        return _structure_name_cache[structure_id]
    name = f"Structure #{structure_id}"
    try:
        with httpx.Client() as client:
            resp = client.get(
                f"{ESI_BASE}/universe/structures/{structure_id}/",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        name = data.get("name") or name
    except Exception:
        pass  # best-effort — an unresolvable structure just shows its raw ID, never blocks the fetch
    _structure_name_cache[structure_id] = name
    return name


def fetch_industry_jobs(character_id: int, access_token: str) -> list[dict]:
    """Fetch this character's reaction jobs (activity_id 11) from ESI, resolving each distinct
    facility to a readable name. Best-effort: returns [] on any failure rather than raising —
    a refresh failing for one character must not block the others."""
    try:
        with httpx.Client() as client:
            resp = client.get(
                f"{ESI_BASE}/characters/{character_id}/industry/jobs/",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            resp.raise_for_status()
            jobs = resp.json()
    except Exception:
        return []

    reaction_jobs = [j for j in jobs if j.get("activity_id") == 11]
    for j in reaction_jobs:
        fac_id = j.get("facility_id")
        j["facility_name"] = _resolve_structure_name(fac_id, access_token) if fac_id else "Unknown"
    return reaction_jobs


@router.post("/api/reactions/jobs/refresh")
def refresh_industry_jobs(context_id: int = Depends(require_context)):
    """Refresh the caller's own characters' cached reaction-job list — only characters that
    have actually granted the industry-jobs scope (opted in via ?reactions=1 login) are
    fetched; others are silently skipped, not an error, since most PI-planner accounts never
    opt into this."""
    ensure_industry_jobs_table()
    con = get_connection()
    try:
        chars = con.execute(
            "SELECT character_id, scopes FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0",
            (context_id,),
        ).fetchall()
    finally:
        con.close()

    refreshed = 0
    for c in chars:
        if "read_character_jobs" not in (c["scopes"] or ""):
            continue
        token = _get_valid_token(c["character_id"])
        if not token:
            continue
        jobs = fetch_industry_jobs(c["character_id"], token)
        con = get_connection()
        try:
            con.execute(
                "INSERT INTO pp_char_industry_jobs (character_id, jobs_json, fetched_at) VALUES (?,?,?) "
                "ON CONFLICT (character_id) DO UPDATE SET jobs_json=excluded.jobs_json, fetched_at=excluded.fetched_at",
                (c["character_id"], _json.dumps(jobs), _time.time()),
            )
            con.commit()
        finally:
            con.close()
        refreshed += 1
    return {"ok": True, "characters_refreshed": refreshed}


def reaction_slots(character_row: dict) -> int:
    """1 base slot + 1/level of Mass Reactions + 1/level of Advanced Mass Reactions, capped at
    the game's real max of 11 (5+5+1)."""
    return min(11, 1 + (character_row.get("mass_reactions") or 0) + (character_row.get("advanced_mass_reactions") or 0))


@router.get("/api/reactions/jobs")
def get_industry_jobs(context_id: int = Depends(require_b0ss)):
    """Personal reaction-job status for the Reactions wizard's dashboard page: currently
    running jobs (from the last refresh), a capacity summary (free slots right now, across
    every character that's opted into tracking), and the per-character opt-in breakdown so the
    UI can offer to connect any character that hasn't opted in yet — a context can hold several
    characters (an account's own alts, or characters from separate EVE accounts logged into the
    same session), and each authorises the tracking scope independently."""
    ensure_industry_jobs_table()
    con = get_connection()
    try:
        chars = con.execute(
            "SELECT character_id, character_name, mass_reactions, advanced_mass_reactions, scopes "
            "FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0",
            (context_id,),
        ).fetchall()
        cached = {r["character_id"]: r for r in con.execute(
            "SELECT character_id, jobs_json, fetched_at FROM pp_char_industry_jobs"
        )}
    finally:
        con.close()

    now = _time.time()
    running: list[dict] = []
    characters: list[dict] = []
    total_slots = 0
    used_slots = 0
    tracked_any = False
    for c in chars:
        opted_in = "read_character_jobs" in (c["scopes"] or "")
        slots = reaction_slots(c)
        if not opted_in:
            characters.append({"character_name": c["character_name"], "tracked": False, "slots": slots})
            continue
        tracked_any = True
        total_slots += slots
        row = cached.get(c["character_id"])
        jobs = _json.loads(row["jobs_json"]) if row else []
        active = [j for j in jobs if j.get("status") in ("active", "paused", "ready")]
        used_slots += len(active)
        characters.append({
            "character_name": c["character_name"], "tracked": True,
            "slots": slots, "free_slots": max(0, slots - len(active)),
        })
        for j in active:
            end = j.get("end_date")
            hours_left = None
            if end:
                try:
                    end_ts = datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()
                    hours_left = round((end_ts - now) / 3600.0, 1)
                except Exception:
                    pass
            running.append({
                "character_name": c["character_name"],
                "product_type_id": j.get("product_type_id"),
                "runs": j.get("runs"),
                "facility_name": j.get("facility_name"),
                "status": j.get("status"),
                "hours_left": hours_left,
            })

    return {
        "tracked": tracked_any,
        "characters": characters,
        "running": sorted(running, key=lambda r: r["hours_left"] if r["hours_left"] is not None else 1e9),
        "total_slots": total_slots,
        "free_slots": max(0, total_slots - used_slots),
    }


# ── Wizard suggestion engine ────────────────────────────────────────────────────────────────
# Two stages, not one monolithic LP: WHAT to run (a knapsack — genuinely an LP's job) and WHO
# runs it (bin-packing onto real characters/slots — not naturally an LP, and keeping it a
# separate greedy step means each stage is small enough to hand-verify on its own).

_MIN_LIQUIDITY = 1000  # order-book depth (both sides) a candidate must clear to be suggested —
# fixed heuristic, not a UI knob, per "use liquidity as a selection filter, don't show it".
_CANDIDATE_POOL_SIZE = 30  # how many of the liquidity-filtered opportunities feed the knapsack


def _character_capacities(context_id: int) -> list[dict]:
    """Per-character free reaction slots right now (capacity minus currently-running jobs) —
    only characters that have opted into job tracking count, since we can't know a non-tracked
    character's current load. Mirrors get_industry_jobs' slot math."""
    ensure_industry_jobs_table()
    con = get_connection()
    try:
        chars = con.execute(
            "SELECT character_id, character_name, mass_reactions, advanced_mass_reactions, scopes "
            "FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0",
            (context_id,),
        ).fetchall()
        cached = {r["character_id"]: r for r in con.execute(
            "SELECT character_id, jobs_json FROM pp_char_industry_jobs"
        )}
    finally:
        con.close()

    result = []
    for c in chars:
        if "read_character_jobs" not in (c["scopes"] or ""):
            continue
        slots = reaction_slots(c)
        row = cached.get(c["character_id"])
        jobs = _json.loads(row["jobs_json"]) if row else []
        used = len([j for j in jobs if j.get("status") in ("active", "paused", "ready")])
        result.append({
            "character_id": c["character_id"], "character_name": c["character_name"],
            "free_slots": max(0, slots - used),
        })
    return result


def _suggest_reactions(context_id: int, isk_budget: float, steps_budget: int) -> dict:
    opportunities = _build_opportunities(context_id)
    candidates = [o for o in opportunities
                  if o["buy_volume"] >= _MIN_LIQUIDITY and o["sell_volume"] >= _MIN_LIQUIDITY
                  and o["top_level_runs"] > 0 and o["net_profit_instant"] > 0]
    # Rank by profit per step (the "least work most profitable" ordering) before truncating to
    # a small pool — keeps the LP tiny regardless of how many opportunities Phase 2 finds.
    candidates.sort(key=lambda o: -(o["net_profit_instant"] / o["top_level_runs"]))
    candidates = candidates[:_CANDIDATE_POOL_SIZE]
    empty = {"suggestions": [], "totals": {
        "isk_committed": 0.0, "net_profit": 0.0, "characters_used": 0, "completion_hours": None}}
    if not candidates:
        return empty

    import highspy  # lazy: only ever needed here, keeps it off the cold-start path (matches app.optimizer)
    n = len(candidates)
    h = highspy.Highs()
    h.silent()
    # x_i in [0,1]: what fraction of candidate i's max achievable batch to actually run — a
    # continuous relaxation, not a strict per-unit integer knapsack, since with only two
    # resource constraints (ISK, steps) the LP optimum is naturally at-or-near integer anyway
    # (at most one variable fractional at the binding constraint), and this stays small/fast
    # and easy to hand-verify, matching this codebase's existing app.optimizer approach.
    hvars = h.addVariables(n, lb=[0.0] * n, ub=[1.0] * n)
    h.maximize(sum(float(c["net_profit_instant"]) * hvars[i] for i, c in enumerate(candidates)))
    h.addConstr(sum(float(c["input_cost"]) * hvars[i] for i, c in enumerate(candidates)) <= float(isk_budget))
    h.addConstr(sum(float(c["top_level_runs"]) * hvars[i] for i, c in enumerate(candidates)) <= float(steps_budget))
    h.run()
    if h.getModelStatus() != highspy.HighsModelStatus.kOptimal:
        return empty

    x = h.getSolution().col_value
    chosen = [(c, xi) for c, xi in zip(candidates, x) if xi > 1e-6]
    chosen.sort(key=lambda cx: -(cx[0]["net_profit_instant"] * cx[1]))
    chosen = chosen[:10]  # the wizard shows up to 10 concrete suggestions
    if not chosen:
        return empty

    # Stage 2: schedule the chosen runs onto real characters, fewest-characters-first. Each
    # candidate is assigned to ONE character's full slot count run in parallel (no splitting a
    # single product across toons — simpler, and matches "least effort" better than maximum
    # parallelism would). Slots are NOT a depleting resource — once a product's batch finishes,
    # the same slots are free again for the next one — so this tracks each character's queued
    # completion time (a schedule), not a one-time slot count that gets used up. Prefers
    # queueing onto an already-touched character UNLESS that would take meaningfully longer
    # than starting fresh on an untouched one — a real trade-off between "fewest characters"
    # and "best time save", not just always picking one. "Meaningfully longer" = more than 2x
    # a fresh character's finish time for this specific product; a fresh character is only
    # worth bringing in when it's a clearly better outcome, not for a marginal time saving.
    _NEW_CHAR_THRESHOLD = 2.0
    chars = _character_capacities(context_id)
    slot_count = {c["character_id"]: c["free_slots"] for c in chars if c["free_slots"] > 0}
    char_names = {c["character_id"]: c["character_name"] for c in chars}
    queued_until = {cid: 0.0 for cid in slot_count}  # hours from now this character is next free
    touched_order: list[int] = []

    def _duration_on(cid: int, runs_needed: int, cycle_time: float) -> float:
        slots_used = max(1, min(slot_count[cid], runs_needed))
        return (runs_needed / slots_used) * (cycle_time / 3600.0)

    suggestions = []
    isk_committed = net_profit = 0.0
    for c, xi in chosen:
        runs_needed = max(1, round(c["top_level_runs"] * xi))
        untouched = [cid for cid in slot_count if cid not in touched_order]
        pick_id = None
        if touched_order:
            best_touched = min(touched_order, key=lambda cid: queued_until[cid])
            touched_finish = queued_until[best_touched] + _duration_on(best_touched, runs_needed, c["cycle_time"])
            if untouched:
                best_untouched = max(untouched, key=lambda cid: slot_count[cid])
                untouched_finish = _duration_on(best_untouched, runs_needed, c["cycle_time"])
                pick_id = best_untouched if untouched_finish * _NEW_CHAR_THRESHOLD < touched_finish else best_touched
            else:
                pick_id = best_touched
        elif untouched:
            pick_id = max(untouched, key=lambda cid: slot_count[cid])
        if pick_id is None:
            continue  # no character has any reaction slots at all — this suggestion can't be scheduled
        if pick_id not in touched_order:
            touched_order.append(pick_id)
        duration_hours = _duration_on(pick_id, runs_needed, c["cycle_time"])
        queued_until[pick_id] += duration_hours

        cost = c["input_cost"] * xi
        reward = c["net_profit_instant"] * xi
        isk_committed += cost
        net_profit += reward
        suggestions.append({
            "type_id": c["type_id"], "name": c["name"],
            "runs": runs_needed,
            "input_cost": round(cost, 2),
            "reward": round(reward, 2),
            "assigned_character": char_names.get(pick_id, "?"),
        })

    completion_hours = max((queued_until[cid] for cid in touched_order), default=0.0)

    return {
        "suggestions": suggestions,
        "totals": {
            "isk_committed": round(isk_committed, 2),
            "net_profit": round(net_profit, 2),
            "characters_used": len(touched_order),
            "completion_hours": round(completion_hours, 1) if suggestions else None,
        },
    }


class SuggestRequest(BaseModel):
    isk_budget: float
    steps_budget: int


@router.post("/api/reactions/suggest")
def suggest_reactions(req: SuggestRequest, context_id: int = Depends(require_b0ss)):
    if req.isk_budget <= 0 or req.steps_budget <= 0:
        return {"suggestions": [], "totals": {
            "isk_committed": 0.0, "net_profit": 0.0, "characters_used": 0, "completion_hours": None}}
    return _suggest_reactions(context_id, req.isk_budget, req.steps_budget)
