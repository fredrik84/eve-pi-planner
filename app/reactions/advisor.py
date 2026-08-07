"""The Reactions wizard suggestion engine — "keep me busy at a good ISK/day".

Split out of app/reactions/jobs.py, which had grown to ~1,500 lines covering three unrelated
jobs: fetching live ESI industry jobs, the persistent slot plan, and this recommendation engine.
This module is the last of those.

Two stages, deliberately not one monolithic solve: WHAT to run (a knapsack over reachable,
priced products — genuinely an LP's job) and WHO runs it (bin-packing onto real characters and
their free reaction slots — not naturally an LP, and keeping it separate means each stage is
small enough to hand-verify).

Depends on app.reactions.jobs one way only (slot capacity, live jobs, per-character reaction
eligibility); jobs.py imports nothing from here, so there is no cycle. `_character_capacities`
deliberately stayed in jobs.py — it is about slots, and the customer-order allocation path needs
it too.
"""

import json as _json
import logging
import math

from fastapi import Depends
from pydantic import BaseModel

from app.sde import get_connection, load_pi_data
from app.cache import cache_get_json, cache_set_json
from app.esi import require_context
from app.market import fetch_daily_volume
from app.reactions._router import router
from app.reactions.graph import (
    _load_goo_and_reached, _value_reaction_batch, _explode_chain_tiers,
    _build_opportunities, _ordered_chain_tiers, _load_reaction_graph, _fuel_block_ids,
)
from app.reactions.settings import effective_reaction_settings
from app.reactions.jobs import (
    ensure_industry_jobs_table, ensure_reaction_assignments_table,
    get_industry_jobs, reaction_capable, reaction_slots, _character_capacities,
    formula_concurrency_caps, _cap_jobs, _parallel_stages_on,
)

log = logging.getLogger(__name__)

# ── Wizard suggestion engine ────────────────────────────────────────────────────────────────
# Two stages, not one monolithic LP: WHAT to run (a knapsack — genuinely an LP's job) and WHO
# runs it (bin-packing onto real characters/slots — not naturally an LP, and keeping it a
# separate greedy step means each stage is small enough to hand-verify on its own).

_MIN_LIQUIDITY = 1000  # order-book depth (both sides) a candidate must clear to be suggested —
# fixed heuristic, not a UI knob, per "use liquidity as a selection filter, don't show it".
_CANDIDATE_POOL_SIZE = 30  # how many of the liquidity-filtered opportunities feed the knapsack
_ABSORB_FRACTION = 0.50  # DEFAULT "Market fill" slider value: a suggested batch, plus what our own
# userbase already has committed, may be up to this fraction of the product's realistic turnover over
# the run period — real daily traded volume (ESI history, Jita) × cadence-days, falling back to the
# standing buy-order-depth snapshot where there's no history. This is the honest "how much actually
# moves in a week" basis; an order-book snapshot alone badly under-states turnover (20% of a snapshot
# collapsed suggestions to a tiny single-product run once the community share was subtracted). It's a
# per-run knob (the frontend "Market fill" slider → absorb_fraction in _suggest_reactions), and also
# the main diversification lever — a thin or already-crowded product runs out of headroom and the LP
# spends the budget elsewhere, so two players stop getting the same list.
_COMMITTED_CACHE_TTL = 90  # matches the opportunities cache — a slow-moving, soft nudge, so a short
# stale window is harmless; keeps the cross-context aggregate off the hot path.


def _committed_production_units() -> dict[int, float]:
    """Across ALL contexts, the output UNITS of each reaction product our own userbase already has
    committed to producing — pending suggestion/order assignments (pp_reaction_assignments) plus
    active/paused/ready ESI reaction jobs (pp_char_industry_jobs). Aggregate counts only, never any
    per-user data (privacy rule: aggregate is fine). The suggestion engine subtracts this from each
    candidate's market-absorption headroom, so a product our community is already flooding into
    Jita's buy orders gets throttled for the next player — that's what makes two different players
    get genuinely different lists. Chain-intermediate rows are included; they're consumed rather
    than sold, so this can slightly over-count pressure on intermediate goods — acceptable for a
    soft nudge. Cached briefly since it changes slowly and only ever nudges."""
    cached = cache_get_json("rx:committed_units")
    if cached is not None:
        return {int(k): v for k, v in cached.items()}
    ensure_industry_jobs_table()
    ensure_reaction_assignments_table()
    runs_by_type: dict[int, float] = {}
    con = get_connection()
    try:
        for r in con.execute("SELECT type_id, SUM(runs) AS runs FROM pp_reaction_assignments GROUP BY type_id"):
            runs_by_type[r["type_id"]] = runs_by_type.get(r["type_id"], 0.0) + (r["runs"] or 0)
        for r in con.execute("SELECT jobs_json FROM pp_char_industry_jobs"):
            for j in _json.loads(r["jobs_json"] or "[]"):
                if j.get("status") not in ("active", "paused", "ready"):
                    continue
                tid = j.get("product_type_id")
                if tid:
                    runs_by_type[tid] = runs_by_type.get(tid, 0.0) + (j.get("runs") or 0)
        out_qty_per_run = {r["output_type_id"]: r["output_qty"]
                           for r in con.execute("SELECT output_type_id, output_qty FROM reactions")}
    finally:
        con.close()
    units = {tid: runs * out_qty_per_run.get(tid, 0.0) for tid, runs in runs_by_type.items()}
    cache_set_json("rx:committed_units", {str(k): v for k, v in units.items()}, ttl=_COMMITTED_CACHE_TTL)
    return units


def _suggest_reactions(context_id: int, isk_budget: float, max_chain_depth: int, cadence_hours: float,
                        material_ids: set[int] | None = None, absorb_fraction: float | None = None) -> dict:
    # How much of the standing Jita buy-order depth a batch is allowed to fill. Defaults to the
    # conservative _ABSORB_FRACTION (sell without moving the price); the advisor's "fill more of the
    # buy book" hint lets the player raise it per re-run to unlock a specific depth-bound product,
    # accepting deeper/cheaper fills. Clamped to (0, 1] — you can't sell into more than the whole book.
    frac = absorb_fraction if (absorb_fraction and absorb_fraction > 0) else _ABSORB_FRACTION
    frac = min(1.0, frac)
    opportunities = _build_opportunities(context_id, allowed_material_ids=material_ids)
    # Needed again in stage 2 to walk each chosen candidate's own formula tree for chain_tiers
    # (the intermediate reactions a multi-tier product needs before its own reaction can even
    # start) — cheap to recompute (resolve_market_data's own cache absorbs the repeat cost).
    _loaded = _load_goo_and_reached(context_id, material_ids)
    reached = _loaded[1] if _loaded else {}
    types = _loaded[4] if _loaded else {}
    candidates = [o for o in opportunities
                  if o["buy_volume"] >= _MIN_LIQUIDITY and o["sell_volume"] >= _MIN_LIQUIDITY
                  and o["top_level_runs"] > 0 and o["net_profit_instant"] > 0
                  and o["steps"] <= max_chain_depth]
    empty = {"suggestions": [], "totals": {
        "isk_committed": 0.0, "isk_budget": isk_budget, "net_profit": 0.0, "net_profit_per_day": None,
        "output_value": 0.0, "output_m3": 0.0, "characters_used": 0, "completion_hours": None,
        "binding": "neither", "absorb_fill_pct": round(frac * 100), "formula_capped": []}}
    if not candidates:
        return empty

    # Rank by profit per step (the "least work most profitable" ordering) and truncate to a small
    # pool BEFORE the cap loop — the batch caps below scale net_profit_instant and top_level_runs
    # by the same factor, so the per-run ratio (this sort key) is cap-invariant and truncating here
    # is identical to truncating after, but it means the market-history fetch only ever runs for the
    # pool (≤ _CANDIDATE_POOL_SIZE types), not every reachable product.
    candidates.sort(key=lambda o: -(o["net_profit_instant"] / o["top_level_runs"]))
    candidates = candidates[:_CANDIDATE_POOL_SIZE]

    # Cap each candidate's usable batch size so a huge, cheap-per-unit chain doesn't get most of
    # the ISK budget allocated to a run count that could never actually finish within the
    # player's chosen cadence (e.g. "weekly") even using every free reaction slot at once — the
    # cap uses the single best character's free-slot count as an upper bound (stage 2 below
    # clamps further to whichever character an assignment actually lands on). Cost/output/profit
    # scale down linearly with runs (unit cost and unit price don't change with batch size).
    chars_for_cap = _character_capacities(context_id)
    max_slots_available = max((c["free_slots"] for c in chars_for_cap), default=0) or 1
    # How many jobs of a product may run at once, given the formulas the account actually holds — a
    # formula is locked into the reactor while a job runs on it. Sparse: a product with no key is a
    # product we have no evidence about, and is never capped (see formula_concurrency_caps).
    formula_caps = formula_concurrency_caps(context_id)
    # One slot model (`reactions_parallel_stages`): a chain's stages reuse each other's slots, and
    # whatever is still idle once every suggestion is placed gets spent widening the slowest steps.
    peak_stages = _parallel_stages_on(context_id)
    committed_units = _committed_production_units()
    # Real daily traded volume (Jita/The Forge) per candidate — the market-absorption base. How much
    # you can realistically sell over the run period is trade VELOCITY × the period, not a single
    # order-book depth snapshot; a snapshot badly under-states a week of turnover. Falls back to
    # standing buy-order depth for anything ESI has no history for (thin/new products).
    cadence_days = cadence_hours / 24.0
    daily_volume = fetch_daily_volume([o["type_id"] for o in candidates])
    capped = []
    for o in candidates:
        cycle_hours = o["cycle_time"] / 3600.0 if o["cycle_time"] else 0
        if cycle_hours <= 0:
            continue
        # Sizing the batch against the best character's free slots is only honest if that many jobs
        # of this product could be installed at once; with one formula it is one job's worth of
        # cadence however many reactors are idle, so the LP never funds runs stage 2 cannot place.
        f_cap = formula_caps.get(o["type_id"])
        max_runs_in_cadence = int(_cap_jobs(f_cap, max_slots_available) * cadence_hours / cycle_hours)
        if max_runs_in_cadence <= 0:
            continue
        # Market-absorption cap: (this batch + what our userbase already committed) must stay under
        # `frac` of the product's realistic turnover over the run period — daily traded volume ×
        # cadence-days where ESI has history, else standing buy-order depth as a fallback. Beyond it
        # you're a large share of the whole market and walk the price down. When this is the tighter
        # of the two caps we record how many MORE runs the cadence would still allow — the advisor
        # turns that into a "sell into more of the market for ~X more" nudge (with an Apply that
        # raises `frac`), but only for a batch that actually presses against that absorption ceiling.
        vol = daily_volume.get(o["type_id"])
        absorb_base = (vol * cadence_days) if vol else (o["buy_volume"] or 0.0)
        committed = committed_units.get(o["type_id"], 0.0)
        out_per_run = o["output_qty"] / o["top_level_runs"] if o["top_level_runs"] else 0.0
        absorb_units = frac * absorb_base - committed
        max_runs_absorb = int(absorb_units / out_per_run) if out_per_run > 0 else 0
        hard_max_runs = min(max_runs_in_cadence, max_runs_absorb)
        if hard_max_runs <= 0:
            continue
        # Extra runs the cadence would permit if the player accepted taking a bigger market share —
        # only nonzero when absorption (not cadence) is what bound this batch.
        absorb_extra_runs = max(0, max_runs_in_cadence - hard_max_runs)
        per_run_profit = o["net_profit_instant"] / o["top_level_runs"] if o["top_level_runs"] else 0.0
        # The fill fraction that would let THIS product reach its cadence cap — what the advisor's
        # Apply jumps `frac` to. Capped at 1.0 (can't sell into more than the whole market).
        unlock_frac = ((max_runs_in_cadence * out_per_run + committed) / absorb_base) if absorb_base > 0 else frac
        c_absorb_unlock_frac = min(1.0, max(frac, unlock_frac))
        if hard_max_runs >= o["top_level_runs"]:
            c2 = dict(o)
        else:
            scale = hard_max_runs / o["top_level_runs"]
            c2 = dict(o)
            c2["top_level_runs"] = hard_max_runs
            c2["output_qty"] = o["output_qty"] * scale
            c2["input_cost"] = o["input_cost"] * scale
            c2["net_profit_instant"] = o["net_profit_instant"] * scale
            c2["shipping_volume_m3"] = o["shipping_volume_m3"] * scale
            c2["instant_sell_value"] = o["instant_sell_value"] * scale
        # Carried through stages 1→2 and onto the suggestion so _build_advisor can size the hint;
        # valued at the current buy price (optimistic — the deeper fills actually sell for less,
        # which the advisor copy flags), mirroring how align_extra_reward is also a linear estimate.
        c2["_absorb_extra_runs"] = absorb_extra_runs
        c2["_absorb_extra_reward"] = absorb_extra_runs * per_run_profit
        c2["_absorb_unlock_frac"] = c_absorb_unlock_frac
        # Whether the formula count — not the free slots — is what held this batch down. Recorded
        # HERE because it is here that it binds: by the time stage 2 sizes the allocation, the runs
        # have already been cut to what the capped job count can finish, so the same comparison
        # there would find nothing to report and the user would be told nothing at all.
        c2["_formula_cap"] = f_cap if (f_cap and f_cap < max_slots_available) else None
        capped.append(c2)
    candidates = capped
    if not candidates:
        return empty

    import highspy  # lazy: only ever needed here, keeps it off the cold-start path (matches app.optimizer)
    n = len(candidates)
    h = highspy.Highs()
    h.silent()
    # x_i in [0,1]: what fraction of candidate i's (cadence-capped) max achievable batch to
    # actually run — a continuous relaxation, not a strict per-unit integer knapsack, since with
    # only ISK as a resource constraint the LP optimum is naturally at-or-near integer anyway (at
    # most one variable fractional at the ISK cap), and this stays small/fast and easy to
    # hand-verify, matching this codebase's existing app.optimizer approach.
    hvars = h.addVariables(n, lb=[0.0] * n, ub=[1.0] * n)
    h.maximize(sum(float(c["net_profit_instant"]) * hvars[i] for i, c in enumerate(candidates)))
    h.addConstr(sum(float(c["input_cost"]) * hvars[i] for i, c in enumerate(candidates)) <= float(isk_budget))
    # Real reaction slots are ALSO a shared, limited resource across every chosen candidate —
    # the per-candidate cadence cap above only checked each one against the single BEST
    # character's slots in isolation, so the LP could (and did, in a real reported case) fund
    # several products that each look individually cadence-feasible but together demand more
    # slots than actually exist across the account; stage 2's real bin-packing then has no
    # choice but to badly overshoot the chosen cadence on whichever gets scheduled last (a real
    # instance: two suggestions landing at 24d/10d runtimes against a much shorter cadence).
    # Uses the continuous (non-ceiled) slot-demand — runs × cycle_hours ÷ cadence_hours — so it
    # stays linear in x_i; the ceil() rounding that can nudge stage 2's actual slot count up by
    # a fraction per suggestion is a minor, expected difference, not the systemic multi-week
    # overshoot this constraint fixes.
    total_free_slots = sum(c["free_slots"] for c in chars_for_cap)
    slot_demand = [c["top_level_runs"] * (c["cycle_time"] / 3600.0 if c["cycle_time"] else 1.0) / cadence_hours
                   for c in candidates]
    h.addConstr(sum(slot_demand[i] * hvars[i] for i in range(n)) <= float(total_free_slots))
    h.run()
    if h.getModelStatus() != highspy.HighsModelStatus.kOptimal:
        return empty

    x = h.getSolution().col_value
    chosen = [(c, xi) for c, xi in zip(candidates, x) if xi > 1e-6]
    chosen.sort(key=lambda cx: -(cx[0]["net_profit_instant"] * cx[1]))
    chosen = chosen[:10]  # the wizard shows up to 10 concrete suggestions
    if not chosen:
        return empty

    # Stage 2 below allocates real slots in ASCENDING ideal-slot-need order (smallest first),
    # not this profit order — letting the biggest, most profit-heavy candidate go first would
    # let it greedily claim its ENTIRE ideal slot count, leaving only rounding scraps for
    # smaller candidates; a small candidate losing even 1 slot to ceil() rounding can lose HALF
    # its allocation (a real reported case: needed 2 slots, got 1, runtime nearly doubled),
    # while a big candidate absorbing that same 1-slot shortfall barely moves its own
    # percentage. Smallest-need-first minimizes the worst-case overshoot across the whole set.
    # `suggestions` is re-sorted back to this original profit order before being returned, so
    # display order is unaffected — only the internal allocation order changes.
    def _ideal_slots_for(c, xi):
        runs_needed = max(1, round(c["top_level_runs"] * xi))
        cycle_hours = c["cycle_time"] / 3600.0 if c["cycle_time"] else 1.0
        return max(1, math.ceil(runs_needed * cycle_hours / cadence_hours)) if cadence_hours > 0 else runs_needed

    alloc_order = sorted(chosen, key=lambda cx: _ideal_slots_for(cx[0], cx[1]))

    # Stage 2: allocate real reaction slots to each chosen product, all targeting completion
    # within roughly one cadence period — NOT a queue over unbounded future time (the old model),
    # since everything here is sized to finish around the same ~cadence window. Each suggestion
    # claims `slots_used` of a character's free slots (a one-time budget for this cadence period,
    # not something that frees up mid-period) — using MORE slots for a bigger batch so it still
    # finishes on time, rather than trickling one run at a time through a single slot for weeks.
    # `job_count`/`runs_per_job` are what the player actually installs in-game (one job install
    # per slot); `runs` is just the total for display.
    chars = _character_capacities(context_id)
    remaining_slots = {c["character_id"]: c["free_slots"] for c in chars if c["free_slots"] > 0}
    char_names = {c["character_id"]: c["character_name"] for c in chars}
    touched_chars: set[int] = set()

    suggestions = []
    # Every step (top-level products AND chain tiers) with the character it landed on, for the
    # idle-slot pass below — collected here rather than reconstructed afterwards, so a step can't
    # be left out of the widening while being in the plan.
    widen_steps: list[dict] = []
    formula_capped: list[str] = []      # products running in fewer jobs than the slots would allow
    isk_committed = net_profit = total_output_value = total_output_m3 = 0.0
    max_completion_hours = 0.0
    for c, xi in alloc_order:
        runs_needed = max(1, round(c["top_level_runs"] * xi))
        cycle_hours = c["cycle_time"] / 3600.0 if c["cycle_time"] else 1.0
        ideal_slots = max(1, math.ceil(runs_needed * cycle_hours / cadence_hours)) if cadence_hours > 0 else runs_needed

        available = [cid for cid, free in remaining_slots.items() if free > 0]
        if not available:
            continue  # no character has any reaction slots left at all — this suggestion can't be scheduled
        # Prefer consolidating onto an already-used character (fewer characters touched overall)
        # as long as it still has room; otherwise open a fresh one with the most free slots.
        touched_with_room = [cid for cid in touched_chars if remaining_slots.get(cid, 0) > 0]
        pick_id = max(touched_with_room, key=lambda cid: remaining_slots[cid]) if touched_with_room \
            else max(available, key=lambda cid: remaining_slots[cid])

        # A formula is an item and it is locked while a job runs on it, so however many slots are
        # free, this product runs in as many jobs as there are formulas of it. Deliberately applied
        # to `want_slots` and NOT to `ideal_slots`: the downscale just below keys off ideal_slots,
        # so a capped suggestion has its run count cut to what those jobs can really finish within
        # the cadence instead of keeping the runs and quietly blowing past it.
        f_cap = formula_caps.get(c["type_id"])
        want_slots = _cap_jobs(f_cap, ideal_slots)
        slots_used = min(want_slots, remaining_slots[pick_id])
        # Capped at either end: stage 1 shrank the batch to fit the formulas, or the batch survived
        # that and the job count is being held down here.
        capped_by_formula = c.get("_formula_cap") or (f_cap if (f_cap and f_cap < ideal_slots) else None)
        remaining_slots[pick_id] -= slots_used
        touched_chars.add(pick_id)

        # Stage 1's cadence cap sized every candidate assuming it COULD land on the single best
        # character's free-slot count — but only one candidate ever actually can. Once it's
        # known here which REAL character (and how many of ITS slots) this suggestion landed
        # on, downscale runs_needed (and everything computed from it below, via xi) to what
        # those specific slots can really finish within cadence, instead of keeping the full
        # run count and letting real duration balloon past what was asked for (a real reported
        # case: multiple suggestions each independently sized for a "best" character that only
        # one of them could actually get, landing at 11d4h against a much shorter cadence).
        if slots_used < ideal_slots:
            achievable_runs = max(1, int(slots_used * cadence_hours / cycle_hours))
            xi *= min(1.0, achievable_runs / runs_needed)
            runs_needed = achievable_runs

        runs_per_job = math.ceil(runs_needed / slots_used)
        duration_hours = (runs_needed / slots_used) * cycle_hours
        max_completion_hours = max(max_completion_hours, duration_hours)

        # Chain tiers: any INTERMEDIATE reaction this product's own formula needs (e.g.
        # goo -> Ferrofluid -> this product) — each is a SEPARATE job the player must install
        # and let finish BEFORE the top-level reaction can even start, since the "force real
        # chains" fix means an intermediate is never just bought pre-made. Slots for these come
        # from the SAME character (one suggestion, one character does the whole chain — simpler
        # than spreading it), taken out of whatever's left after the top tier's own allocation.
        chain_tiers = []
        top_via = reached.get(c["type_id"], {}).get("via")
        if top_via:
            # Deepest (closest to raw goo) first — the one the player must react first.
            ordered = _ordered_chain_tiers(top_via["inputs"], runs_needed, reached)
            # Slots this suggestion is holding at its BUSIEST moment. A tier reuses the slots the
            # tier below it has finished with, so what a chain costs the character is the worst
            # tier, not the sum — the same model the assign guard commits under
            # (`_concurrent_load`). Subtracting every tier separately (the old behaviour) charged a
            # 3-stage chain three slots for work that never overlaps, and the character then looked
            # full to every suggestion after it.
            peak_used = slots_used
            for tid, info in ordered:
                t_cycle_hours = info["cycle_time"] / 3600.0 if info["cycle_time"] else 1.0
                t_ideal_slots = max(1, math.ceil(info["runs"] * t_cycle_hours / cadence_hours)) if cadence_hours > 0 else info["runs"]
                # Each tier against its OWN formula count. Never across tiers: tier 0 has finished
                # before tier 1 starts, so one formula can legitimately serve both.
                t_cap = formula_caps.get(tid)
                # A tier may use the slots this suggestion already reserved for its other tiers,
                # so its ceiling is what's left PLUS that reservation.
                headroom = remaining_slots.get(pick_id, 0) + peak_used if peak_stages else remaining_slots.get(pick_id, 0)
                t_slots_used = max(1, min(_cap_jobs(t_cap, t_ideal_slots), headroom))
                if peak_stages:
                    remaining_slots[pick_id] = remaining_slots.get(pick_id, 0) - max(0, t_slots_used - peak_used)
                    peak_used = max(peak_used, t_slots_used)
                else:
                    remaining_slots[pick_id] = remaining_slots.get(pick_id, 0) - t_slots_used
                if t_cap and t_cap < t_ideal_slots:
                    formula_capped.append(types.get(tid, {}).get("name", str(tid)))
                tier_row = {
                    "type_id": tid, "name": types.get(tid, {}).get("name", str(tid)),
                    "runs": info["runs"],
                    "job_count": t_slots_used,
                    "runs_per_job": math.ceil(info["runs"] / t_slots_used),
                    "formula_cap": t_cap,
                }
                chain_tiers.append(tier_row)
                widen_steps.append({
                    "character_id": pick_id, "tier": len(chain_tiers) - 1, "runs": info["runs"],
                    "cycle_hours": t_cycle_hours, "jobs": t_slots_used, "cap": t_cap,
                    "row": tier_row,
                })

        cost = c["input_cost"] * xi
        reward = c["net_profit_instant"] * xi
        output_qty = c["output_qty"] * xi
        output_value = c["instant_sell_value"] * xi
        output_m3 = c["shipping_volume_m3"] * xi
        isk_committed += cost
        net_profit += reward
        total_output_value += output_value
        total_output_m3 += output_m3

        # How much MORE this specific product could use if it were ISK-funded all the way to
        # actually filling its claimed slots for the whole cadence window, instead of finishing
        # early and leaving them idle until the next check-in. Bounded by `top_level_runs` (the
        # true cadence/stock-capped max for this candidate) so this never suggests spending ISK
        # on more than could physically be produced.
        max_runs_per_job_for_cadence = math.floor(cadence_hours / cycle_hours) if cycle_hours > 0 else runs_per_job
        aligned_runs = min(slots_used * max_runs_per_job_for_cadence, c["top_level_runs"])
        align_extra_runs = max(0, aligned_runs - runs_needed)
        align_ratio = aligned_runs / c["top_level_runs"]
        align_extra_isk = round(align_extra_runs * (c["input_cost"] / c["top_level_runs"]), 2) if align_extra_runs > 0 else 0.0
        align_extra_reward = round(align_extra_runs * (c["net_profit_instant"] / c["top_level_runs"]), 2) if align_extra_runs > 0 else 0.0

        # Profit normalized to ISK/day, matching how the PI planner already reports value_per_day
        # — divided by the CADENCE window (not this suggestion's own, possibly-shorter runtime),
        # since a batch that finishes early just leaves its claimed slots idle until the next
        # cadence check-in; the cadence-normalized rate is the honest "average ISK/day this
        # delivers" including that idle time (the align hint above already targets closing this
        # exact gap by suggesting more spend to fill the whole window).
        profit_per_day = round(reward / (cadence_hours / 24), 2) if cadence_hours > 0 else None

        top_row = {
            "type_id": c["type_id"], "name": c["name"],
            "runs": runs_needed,
            "job_count": slots_used,
            "runs_per_job": runs_per_job,
            "input_cost": round(cost, 2),
            "reward": round(reward, 2),
            "profit_per_day": profit_per_day,
            "output_qty": round(output_qty, 1),
            "output_value": round(output_value, 2),
            "output_m3": round(output_m3, 1),
            "runtime_hours": round(duration_hours, 1),
            "align_extra_isk": align_extra_isk,
            "align_extra_reward": align_extra_reward,
            # Absolute (not delta) values for applying the alignment in one click — the frontend
            # swaps a suggestion's displayed fields to these wholesale rather than re-running the
            # whole optimizer, so clicking "align" only ever changes THIS product, nothing else.
            "aligned_runs": aligned_runs,
            "aligned_runs_per_job": max_runs_per_job_for_cadence,
            "aligned_input_cost": round(c["input_cost"] * align_ratio, 2),
            "aligned_reward": round(c["net_profit_instant"] * align_ratio, 2),
            "aligned_profit_per_day": round(c["net_profit_instant"] * align_ratio / (cadence_hours / 24), 2) if cadence_hours > 0 else None,
            "aligned_output_qty": round(c["output_qty"] * align_ratio, 1),
            "aligned_output_value": round(c["instant_sell_value"] * align_ratio, 2),
            "aligned_output_m3": round(c["shipping_volume_m3"] * align_ratio, 1),
            # Market-depth headroom (see the absorption cap in stage 1): how many more runs (and
            # roughly how much more profit, at current buy price) the cadence would still allow if
            # the player were willing to fill more of the standing buy orders. Nonzero only when
            # this batch is capped by buy-order depth, i.e. it presses against the good depth.
            "absorb_fill_pct": round(frac * 100),
            "absorb_next_pct": round(c.get("_absorb_unlock_frac", frac) * 100),
            "absorb_extra_runs": int(c.get("_absorb_extra_runs", 0)),
            "absorb_extra_reward": round(c.get("_absorb_extra_reward", 0.0), 2),
            "assigned_character": char_names.get(pick_id, "?"),
            "assigned_character_id": pick_id,
            "chain_tiers": chain_tiers,
            # How many formulas of this product the account has evidence of, when that is what held
            # the job count down — so the UI can say WHY ten free slots are being used as one,
            # rather than leaving it looking broken. None = not capped / nothing known.
            "formula_cap": capped_by_formula,
        }
        suggestions.append(top_row)
        # The top-level product's own tier sits ABOVE every chain tier (same numbering the assign
        # path stores as `tier_order`), so widening it competes only with other suggestions' top
        # tiers on this character, not with its own intermediates.
        widen_steps.append({
            "character_id": pick_id, "tier": len(chain_tiers), "runs": runs_needed,
            "cycle_hours": cycle_hours, "jobs": slots_used, "cap": f_cap, "row": top_row,
        })
        if capped_by_formula:
            formula_capped.append(c["name"])

    # Reactors nobody claimed are reactors doing nothing while a chain waits on one job. Hand them
    # to the steps that finish soonest for it (`_widen_to_idle_slots`), then write the new job
    # counts back onto the rows the player actually installs. Runs, cost and profit are untouched —
    # this only splits the same work across more reactors, so the batch lands sooner.
    widened = _widen_to_idle_slots(widen_steps, remaining_slots) if peak_stages else 0
    if widened:
        max_completion_hours = 0.0
        for s in widen_steps:
            row, jobs = s["row"], s["jobs"]
            row["job_count"] = jobs
            row["runs_per_job"] = math.ceil(s["runs"] / jobs)
        for s in widen_steps:
            if "reward" in s["row"]:            # a top-level suggestion, not a chain tier
                s["row"]["runtime_hours"] = round((s["runs"] / s["jobs"]) * s["cycle_hours"], 1)
                max_completion_hours = max(max_completion_hours, (s["runs"] / s["jobs"]) * s["cycle_hours"])

    # Built in allocation order (smallest slot-need first, see alloc_order above) — restore
    # profit-descending order for display, matching what the LP itself ranked as most valuable.
    suggestions.sort(key=lambda s: -s["reward"])

    # "isk" = spent (near enough) the whole budget; "neither" = ran out of profitable, liquid,
    # within-chain-depth/cadence candidates before using it all — raising the ISK budget further
    # won't help, there's nothing more suitable to spend it on right now.
    binding = "isk" if isk_committed >= 0.97 * isk_budget else "neither"

    # Formulas this whole batch needs and the account does not hold — the wizard is the entry point
    # that produces the deepest chains, so it is the one that would otherwise hand the player a
    # sub-reaction they cannot install (Reinforced Carbon Fiber via an undeclared Carbon Fiber, the
    # case that started this). Every step counts, top-level and tier alike; nothing here changes a
    # suggestion, its cost, or the shopping list. See app/reactions/library.py.
    from app.reactions.library import missing_formulas, wanted_from_sequence
    wanted = wanted_from_sequence(
        [{"type_id": s["type_id"], "runs": s["runs"]} for s in suggestions]
        + [t for s in suggestions for t in s["chain_tiers"]])

    return {
        "suggestions": suggestions,
        "missing_formulas": missing_formulas(context_id, wanted),
        "totals": {
            "isk_committed": round(isk_committed, 2),
            "isk_budget": isk_budget,
            "net_profit": round(net_profit, 2),
            "net_profit_per_day": round(net_profit / (cadence_hours / 24), 2) if cadence_hours > 0 and suggestions else None,
            "output_value": round(total_output_value, 2),
            "output_m3": round(total_output_m3, 1),
            "characters_used": len(touched_chars),
            "completion_hours": round(max_completion_hours, 1) if suggestions else None,
            "binding": binding,
            "absorb_fill_pct": round(frac * 100),
            # Names of the products (and chain tiers) whose job count was held down by how many
            # formulas the account holds rather than by free slots — one UI line's worth of "why".
            "formula_capped": formula_capped,
            # Extra jobs handed to otherwise-idle reactors so a step finishes sooner. Reported so
            # the extra job counts read as a deliberate choice rather than the tool overbooking.
            "idle_slots_used": widened,
        },
    }


class SuggestRequest(BaseModel):
    isk_budget: float
    max_chain_depth: int = 2
    cadence_hours: float = 168.0  # default weekly — how long you want a batch to run before checking back in
    material_ids: list[int] | None = None  # None/empty = no restriction, every priced material usable
    absorb_fraction: float | None = None  # None = default _ABSORB_FRACTION; the "fill more of the buy
    # book" advisor Apply raises it to unlock a depth-bound product. Clamped to (0, 1] server-side.


def _widen_to_idle_slots(steps: list[dict], free_by_char: dict[int, int]) -> int:
    """Spend reactors that would otherwise sit idle on the steps that finish soonest for it.

    A step is `{character_id, tier, runs, cycle_hours, jobs, cap}` and is MUTATED in place: only
    `jobs` moves. Returns how many extra jobs were handed out.

    Why this exists: every step is sized to finish inside the CADENCE window
    (`ceil(runs x cycle / cadence)`), which is the right question for "how much work fits" and the
    wrong one for "how fast does it finish". A 3-stage chain sized that way runs one reactor at a
    time for three cadence windows while seven sit empty — and because the stages are sequential,
    the wait is the whole chain's, not one step's. So once every suggestion has a home, whatever
    capacity is STILL idle goes to whichever step the extra job helps most.

    Three rules keep this honest:

      * **Idle capacity only.** It runs after allocation, so it can never take a slot from a
        suggestion that wanted one. Nothing here changes runs, cost, profit or what gets suggested
        — only how many jobs one step is split across, and therefore when it finishes.
      * **Slots are counted per TIER**, like everywhere else: a character's load is its worst tier,
        so widening a tier that is not the busiest one is free and costs no idle slot at all.
      * **Never past the formulas held** (`cap`), and never more jobs than runs — a job needs a
        formula to install on and at least one run to do.
    """
    handed_out = 0
    by_char: dict[int, list[dict]] = {}
    for s in steps:
        by_char.setdefault(s["character_id"], []).append(s)

    def _hours(s, jobs):
        return math.ceil(s["runs"] / max(1, jobs)) * s["cycle_hours"]

    for cid, own in by_char.items():
        idle = max(0, int(free_by_char.get(cid, 0)))
        # NOT `while idle > 0`: widening a tier below the busiest one costs no idle reactor at all
        # (it fits inside slots this character is already holding for its peak tier), and that is
        # worth doing on a character with nothing spare. The loop still terminates — every pass
        # either adds a job, bounded by runs and the formula cap, or breaks.
        while True:
            load: dict[int, int] = {}
            for s in own:
                load[s["tier"]] = load.get(s["tier"], 0) + s["jobs"]
            peak = max(load.values(), default=0)
            best, best_gain, best_cost = None, 0.0, 0
            for s in own:
                cap = s.get("cap")
                if s["jobs"] >= s["runs"] or (cap and s["jobs"] >= cap):
                    continue
                # A tier below the peak has room inside slots the character is already holding, so
                # widening it costs nothing; only pushing the busiest tier higher spends an idle
                # reactor.
                cost = 1 if load[s["tier"]] + 1 > peak else 0
                if cost > idle:
                    continue
                gain = _hours(s, s["jobs"]) - _hours(s, s["jobs"] + 1)
                # Free widening wins ties: it buys time back without spending capacity.
                if gain > best_gain or (gain == best_gain and gain > 0 and cost < best_cost):
                    best, best_gain, best_cost = s, gain, cost
            if best is None or best_gain <= 0:
                break
            best["jobs"] += 1
            handed_out += 1
            idle -= best_cost
    return handed_out


_BUDGET_SENSITIVITY_STEP = 0.10  # "what if you raised your ISK budget by 10%?"


def _build_advisor(context_id: int, isk_budget: float, max_chain_depth: int, cadence_hours: float,
                    material_ids: set[int] | None, current_profit: float, current_profit_per_day: float | None,
                    current_binding: str, suggestions: list[dict], absorb_fraction: float | None = None) -> dict:
    """Cheap, easily-computable "how could this be better" hints — not a full analysis, just the
    obvious low-effort wins: whether a bit more ISK would actually buy meaningfully more profit
    right now (vs. there being nothing left worth spending it on within the current chain-depth/
    cadence/material limits), per-product cadence-alignment gaps (a suggestion that finishes
    early and leaves its claimed slots idle for the rest of the cadence window, for want of a bit
    more ISK to keep them running), and which excluded fuel blocks would be worth allowing back
    in. Deliberately does NOT suggest skill training — unlike every other hint here, training a
    reaction skill takes real days/weeks in-game, not something this session can act on, so it
    read as permanent background noise rather than a real "low-effort win" (explicit user
    feedback)."""
    # Budget sensitivity: only worth suggesting "raise your ISK budget" when ISK is actually the
    # thing holding this back right now (current_binding == "isk") — if the current run already
    # left ISK unspent ("neither"), the real limit is something else (chain depth, cadence,
    # material filter, or simply no more profitable/liquid candidates), and more ISK wouldn't
    # help; recommending it anyway would be confusing/wrong advice.
    budget_hint = None
    if current_binding == "isk" and current_profit > 0:
        bigger = _suggest_reactions(context_id, isk_budget * (1 + _BUDGET_SENSITIVITY_STEP),
                                     max_chain_depth, cadence_hours, material_ids, absorb_fraction)
        extra_profit = bigger["totals"]["net_profit"] - current_profit
        if extra_profit > current_profit * 0.01:
            budget_hint = {
                "extra_isk": round(isk_budget * _BUDGET_SENSITIVITY_STEP, 2),
                "extra_profit": round(extra_profit, 2),
            }

    # Per-product cadence-alignment gaps (see the align_extra_isk/align_extra_reward computed
    # alongside each suggestion in _suggest_reactions) — worth a mention only when it's a
    # meaningful amount of profit, not a rounding-sized sliver.
    align_hints = [
        {"name": s["name"], "extra_isk": s["align_extra_isk"], "extra_reward": s["align_extra_reward"]}
        for s in suggestions if s.get("align_extra_isk", 0) > 0 and s["align_extra_reward"] > current_profit * 0.01
    ]

    # (There used to be per-product "fill more of the buy book" nudges here, each with an Apply that
    # bumped the market-fill fraction. Removed once the "Market fill" slider shipped: the slider is
    # the single, honest control now, and the nudges perpetually pushed toward 100% — i.e. "be the
    # entire market's weekly trade" — which is bad advice under volume-based sizing and made the
    # slider's default never settle, since resetting it just re-triggered the nudges.)

    # Fuel-block breadth: if the caller restricted which racial fuel blocks to use (the advanced
    # material filter, e.g. "only Oxygen — that's my cheap local one"), quantify what re-adding
    # each EXCLUDED one would actually be worth, in the same ISK/day terms the rest of this tool
    # already reports profit in — turns "I locked myself to one fuel block" from a guess into a
    # real number ("+12% ISK/day if you also used Hydrogen Fuel Block") the player can weigh
    # against how much of a hassle sourcing that second variant actually is for them.
    fuel_block_hints = []
    if material_ids is not None and current_profit_per_day:
        con = get_connection()
        try:
            reactions_by_output, inputs_by_reaction = _load_reaction_graph(con)
        finally:
            con.close()
        all_fuel_blocks = _fuel_block_ids(inputs_by_reaction, reactions_by_output, load_pi_data()["types"])
        excluded = {tid: name for tid, name in all_fuel_blocks.items() if tid not in material_ids}
        for tid, name in excluded.items():
            widened = _suggest_reactions(context_id, isk_budget, max_chain_depth, cadence_hours,
                                          material_ids | {tid}, absorb_fraction)
            widened_per_day = widened["totals"].get("net_profit_per_day") or 0.0
            extra_per_day = widened_per_day - current_profit_per_day
            if extra_per_day > current_profit_per_day * 0.01:
                fuel_block_hints.append({
                    "type_id": tid, "name": name,
                    "extra_isk_per_day": round(extra_per_day, 2),
                    "extra_pct": round(100 * extra_per_day / current_profit_per_day, 1),
                })
        fuel_block_hints.sort(key=lambda h: -h["extra_isk_per_day"])

    return {"budget_hint": budget_hint, "align_hints": align_hints, "fuel_block_hints": fuel_block_hints}


@router.post("/api/reactions/suggest")
def suggest_reactions(req: SuggestRequest, context_id: int = Depends(require_context)):
    if req.isk_budget <= 0 or req.max_chain_depth <= 0 or req.cadence_hours <= 0:
        return {"suggestions": [], "totals": {
            "isk_committed": 0.0, "isk_budget": req.isk_budget, "net_profit": 0.0, "net_profit_per_day": None,
            "output_value": 0.0, "output_m3": 0.0, "characters_used": 0, "completion_hours": None, "binding": "neither", "formula_capped": []},
            "advisor": {"budget_hint": None, "align_hints": [], "fuel_block_hints": []}}
    material_ids = set(req.material_ids) if req.material_ids else None
    result = _suggest_reactions(context_id, req.isk_budget, req.max_chain_depth, req.cadence_hours,
                                 material_ids, req.absorb_fraction)
    result["advisor"] = _build_advisor(context_id, req.isk_budget, req.max_chain_depth, req.cadence_hours,
                                        material_ids, result["totals"]["net_profit"],
                                        result["totals"].get("net_profit_per_day"), result["totals"]["binding"],
                                        result["suggestions"], req.absorb_fraction)
    return result
