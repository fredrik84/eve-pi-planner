"""Industry planner — Phase 2: demand aggregation + the parallel-slot scheduler.

Two problems on top of the Phase-1 make-or-buy cost core (graph.py):

1. DEMAND AGGREGATION across a whole queue of build orders. Rather than exploding each order into
   its own tree (which double-counts shared components), we do a single MRP-style explosion in
   low-level-code order: sum ALL demand for a type before computing its runs, so a component two
   capitals share is built in ONE combined batch. Output rounding (a reaction makes 2/run, an odd
   demand) leaves excess, carried in a stock ledger and reported per type.

2. SCHEDULING the resulting jobs into limited parallel slots. A resource-constrained list
   scheduler over the job DAG: two independent slot pools (manufacturing + reaction), priority =
   critical-path length, a job is ready the moment its BUILT inputs are complete (BOUGHT inputs are
   available at t=0). This front-loads every leaf build in parallel and pulls later-tier work
   forward as inputs land — reporting makespan, per-wave timeline, and slot occupancy.

Pure/testable and deliberately I/O-free: every function takes prebuilt graphs + params, and this
module owns no endpoint, DB handle or market lookup. Callers (graph.py's /api/industry/plan and
orders.py's queue endpoints) resolve the real data and hand it in.
"""
import copy
import math
from collections import defaultdict
from dataclasses import dataclass

from app.industry.graph import (
    BuildParams, blueprint_summary, collect_reachable, effective_material_qty,
    reaction_policy_report, resolve_unit_costs,
)


# ── Demand aggregation (MRP explosion) ────────────────────────────────────────────────────────

def _depths(targets: list[int], mfg: dict, rx: dict) -> dict[int, int]:
    """Low-level code per type: 0 for a target, and a built type's inputs sit one level below the
    deepest consumer that reaches them. Processing built types in ascending depth guarantees every
    consumer's demand is accumulated before the component's runs are computed. Cycle-guarded."""
    depth: dict[int, int] = {}

    def visit(tid: int, d: int, stack: frozenset):
        if tid in stack:                    # cycle guard
            return
        if d <= depth.get(tid, -1):         # already recorded at an equal-or-deeper level
            return
        depth[tid] = d
        recipe = mfg.get(tid) or rx.get(tid)
        if recipe:
            inner = stack | {tid}
            for inp in recipe["inputs"]:
                visit(inp["type_id"], d + 1, inner)

    for t in targets:
        visit(t, 0, frozenset())
    return depth


def marginal_threshold(memo: dict, targets: list[tuple[int, int]], params: BuildParams) -> float:
    """ISK a component must save before building it is worth a job.

    `max(marginal_pct_of_total % of the whole build, min_saving_isk)` — the percentage governs big
    builds, the absolute floor governs small ones, so one rule covers an Augoror and a Revelation.
    Shared by the decision itself and the figure reported back to the UI so the two can't drift.

    NOTE `build_unit_cost` is present-but-None when a material couldn't be priced, so a
    `.get(key, 0.0)` default does NOT save us here — it must be coerced with `or 0.0`.
    """
    total_build_value = sum(((memo.get(t) or {}).get("build_unit_cost") or 0.0) * qty
                            for t, qty in targets)
    pct_abs = (params.marginal_pct_of_total / 100.0 * total_build_value
               if params.marginal_pct_of_total > 0 else 0.0)
    return max(pct_abs, params.min_saving_isk)


def aggregate_demand(targets: list[tuple[int, int]], memo: dict, mfg: dict, rx: dict,
                     params: BuildParams, on_hand: dict[int, float] | None = None,
                     pools: dict[str, int] | None = None) -> dict[int, dict]:
    """Combined per-type demand across all order targets. Targets are always built; every other
    type follows its make-or-buy decision in `memo`. Returns {type_id: {activity, build, gross,
    net, runs, produced, leftover, output_qty, bought_for_speed}}.

    TIME-AWARE make-or-buy: when `params.max_build_hours > 0`, a component the cost engine would
    build is flipped to BUY if producing its whole batch would take longer than that wall-clock cap
    (runs × cycle ÷ pool slots) and it's purchasable — so the slow bulk marathons (thousands of
    reaction runs) come off the market instead of dominating the makespan, while everything that
    builds fast is still produced. Prioritizes time-to-completion over cost, per the design goal."""
    on_hand = dict(on_hand or {})
    pools = pools or {}
    target_ids = {t for t, _ in targets}
    depth = _depths([t for t, _ in targets], mfg, rx)

    gross: dict[int, float] = defaultdict(float)
    for tid, qty in targets:
        gross[tid] += qty

    def is_built(tid: int) -> bool:
        if tid in target_ids:
            return (tid in mfg) or (tid in rx)          # a target is built if it *can* be
        return memo.get(tid, {}).get("decision") == "build"

    # Process built types shallowest-first so demand is fully accumulated before runs are computed.
    built = [tid for tid in depth if is_built(tid)]
    built.sort(key=lambda t: depth[t])

    marginal_abs = marginal_threshold(memo, targets, params)

    result: dict[int, dict] = {}
    flipped: set[int] = set()             # bought for speed (slow to build)
    flipped_marginal: set[int] = set()    # bought because building saves a trivial amount
    marginal_saving: dict[int, float] = {}   # ISK building it WOULD have saved (negative = costs more)
    blueprint_cost_total = 0.0
    for tid in built:
        recipe = mfg.get(tid) or rx.get(tid)
        activity = "manufacturing" if tid in mfg else "reaction"
        mult, _tm = params.struct_mults_for(tid, activity)
        output_qty = recipe["output_qty"]
        net = max(0.0, gross[tid] - on_hand.get(tid, 0.0))
        runs = max(0, math.ceil(net / output_qty)) if net > 0 else 0
        produced = runs * output_qty
        # Demand is aggregated BEFORE jobs are split, so a per-job ME/TE has no meaning here: what
        # this batch's materials cost is what the copies it consumes come to, runs-weighted. Passing
        # the run count is what lets `me_te_for` weigh exactly those copies (and price the runs no
        # owned copy covers off the one the plan would buy) instead of crediting the whole batch to
        # the best copy in the drawer.
        me, te = params.me_te_for(tid, activity, runs)

        # Time-aware flip: buy this batch instead of building if it's slow AND purchasable (never
        # flip a target — the user asked to build that).
        if (params.max_build_hours > 0 and runs > 0 and tid not in target_ids
                and tid not in params.force_build_ids
                and (memo.get(tid) or {}).get("buy_unit_cost") is not None):
            P = max(1, pools.get(activity, 1))
            wall_h = runs * recipe["base_time"] * (1 - te / 100.0) / P / 3600.0
            if wall_h > params.max_build_hours:
                flipped.add(tid)          # falls through to the bought loop; inputs not exploded
                continue

        # Blueprint you don't own: a COPY is a consumable, so its price is a genuine cost of this
        # batch and counts against the saving like any other input.
        #
        # What it must NOT do is decide for you. Blueprint ownership is read per character, and only
        # characters that granted the blueprints scope are visible — a corp-held print, or an alt
        # that was never connected, looks identical to "you don't own one". Acting on that absence
        # would silently refuse to build things the user owns the print for. So a missing blueprint
        # is priced and surfaced, never used to force a buy.
        bp = params.bp_acquire.get(tid)
        bp_cost = 0.0
        bp_buy = None
        # What the account's OWN blueprints cover. An original covers everything; copies cover the
        # runs they carry — ALL of them, summed. Owning a 4-run copy for a 20-run batch is not "you
        # have the blueprint", it is sixteen runs you still have to find; and owning fourteen copies
        # worth 212 runs is not five runs, which is what counting only the best copy reported.
        own = (params.owned or {}).get(tid) or {}
        covered = (runs if own.get("kind") == "bpo" else
                   min(runs, max(0, int(own.get("runs") or 0))) if own else 0)
        short = max(0, runs - covered)
        if bp and short > 0 and bp["kind"] != "bpo_only":
            # Priced from the real listings: a copy carries a fixed number of runs and one contract
            # is one item, so a 40-run batch off 10-run copies means buying four of them.
            from app.industry.bpc import cost_for_runs
            bp_buy = cost_for_runs(bp, short)
            bp_cost = bp_buy["cost"]
            blueprint_cost_total += bp_cost

        # Marginal-saving flip: buy if building saves a trivial amount — negligible vs the whole
        # product, or a tiny % of the component's own buy price. Not worth a job.
        node = memo.get(tid) or {}
        buc, byc = node.get("build_unit_cost"), node.get("buy_unit_cost")
        if runs > 0 and tid not in target_ids and buc is not None and byc is not None:
            total_saving = (byc - buc) * net - bp_cost      # the print is part of building it
            total_buy = byc * net
            # `<= 0` matters now that the blueprint counts: the cost engine only ever proposes
            # building when the MATERIALS are cheaper, so the saving used to be positive by
            # construction and the old guard required that. Add the price of a print you don't own
            # and it can go negative — building genuinely costs more than buying — which is the
            # clearest possible reason to buy, and the old guard skipped exactly that case.
            below_abs = marginal_abs > 0 and total_saving < marginal_abs
            below_pct = (params.min_saving_pct > 0 and total_buy > 0
                         and total_saving < params.min_saving_pct / 100.0 * total_buy)
            if (total_saving <= 0 or below_abs or below_pct) and tid not in params.force_build_ids:
                flipped_marginal.add(tid)
                # What the user gives up by taking the shortcut. Reported per material so "low
                # saving" is an amount they can judge, not a verdict they have to trust.
                marginal_saving[tid] = round(total_saving, 2)
                continue

        result[tid] = {
            "type_id": tid, "activity": activity, "build": True,
            "gross": gross[tid], "net": net, "runs": runs, "produced": produced,
            "leftover": produced - net, "output_qty": output_qty, "bought_for_speed": False,
            "blueprint_cost": bp_cost, "blueprint_buy": bp_buy,
            # Runs this batch needs that the account's own blueprint cannot cover, so the plan can
            # say "you hold a 4-run copy and this is 20 runs" instead of implying you're ready.
            "runs_short": short if own else 0,
        }
        for inp in recipe["inputs"]:
            gross[inp["type_id"]] += effective_material_qty(inp["quantity"], runs, me, mult)

    # Bought / raw types: whatever gross demand is left that isn't built.
    for tid, qty in gross.items():
        if tid in result or qty <= 0:
            continue
        result[tid] = {"type_id": tid, "activity": None, "build": False, "gross": qty,
                       "net": qty, "runs": 0, "produced": 0, "leftover": 0, "output_qty": 0,
                       "bought_for_speed": tid in flipped, "bought_marginal": tid in flipped_marginal,
                       "marginal_saving": marginal_saving.get(tid),
                       # Built only because the user overrode the shortcut — not the engine's call.
                       "forced_build": False}
    return result


# ── Scheduling ────────────────────────────────────────────────────────────────────────────────

@dataclass
class Task:
    task_id: str
    type_id: int
    activity: str          # "manufacturing" | "reaction" → which slot pool
    runs: int
    duration: float        # seconds
    start: float = 0.0
    end: float = 0.0
    slot: int = 0
    # What the SCHEDULER treats as this job's identity. Normally the type — one shared batch per
    # component — but when orders are planned separately it is namespaced per order, so an order's
    # jobs can never satisfy a different order's dependency. `type_id` stays the real type, because
    # every name, price and progress lookup keys on that.
    key: object = None
    order_id: int | None = None
    # The research of the blueprint COPY this job runs on. Jobs of the same type in one batch can
    # legitimately differ — that is the point of consuming the best copies first — so the number is
    # per job, not per type. None for a reaction (no blueprint).
    me: float | None = None
    te: float | None = None
    # Why this job is the length it is: the window it had, and what set it. Carried to the UI
    # because "why is this 2h 32m when everything else is 5h" is otherwise unanswerable without
    # reading the scheduler, and the answer is usually "something needs it sooner".
    why: dict | None = None

    def sched_key(self):
        return self.type_id if self.key is None else self.key


def _balanced(total: int, n: int) -> list[int]:
    base, extra = divmod(total, n)
    return [base + 1 if i < extra else base for i in range(n)]


def _copy_limits(params, tid: int, activity: str,
                 runs: int) -> tuple[list[dict], int | None, int | None]:
    """(owned copies best first, the most runs any ONE copy may carry, runs per BOUGHT copy).

    The copies are the account's owned ones best-researched first; anything the batch needs past
    them comes off copies the plan buys, which is one entry of `runs_per_copy` repeated as needed.
    The limit is None when nothing binds (an owned ORIGINAL, or an unowned print with no listing to
    tell us how big a copy is — in which case the old, uncapped behaviour is what we keep).
    """
    if activity != "manufacturing":
        return [], None, None
    copies = list(params.copies_for(tid, activity))
    acq = (params.bp_acquire or {}).get(tid) or {}
    buy_runs = (int(acq["runs_per_copy"])
                if acq.get("kind") == "bpc" and (acq.get("runs_per_copy") or 0) > 0 else None)
    limits: list[int] = []
    for c in copies:
        if (c.get("runs") if c.get("runs") is not None else -1) < 0:
            return copies, None, None            # an original: no per-job run limit at all
        limits.append(int(c.get("runs") or 0))
    if runs > sum(limits) and buy_runs:          # the rest is bought, one contract = one copy
        limits.append(buy_runs)
    limits = [n for n in limits if n > 0]
    # Nothing owned and nothing listed = nothing known, which is where the cap came in: leave the
    # job uncapped, exactly as it was before copies were modelled at all. With copies in hand but no
    # listing to size a bought one, the copies you hold are the only evidence there is — assume a
    # bought copy is no larger, since an over-split batch is merely inefficient and a job bigger
    # than its copy cannot be installed at all.
    return copies, (max(limits) if limits else None), buy_runs


def _print_limits(params, tid: int, activity: str, runs: int) -> tuple[int | None, bool]:
    """(how many physical PRINTS this type can run jobs off, whether more can be bought).

    A blueprint is an item, and it is LOCKED while a job is installed on it: one print runs one job
    at a time, however many slots are free and however many runs the batch needs. The scheduler
    modelled runs and slots and never this, so one owned 4-run copy planned four simultaneous jobs
    and one owned BPO planned ten — unlimited *runs* read as unlimited *parallelism*.

    The count is what the account holds (`copies` is one entry per item, an ORIGINAL included — it
    never runs out, but it is still one item) plus the copies the plan already buys to cover the runs
    those don't. `can_buy` says whether more prints are purchasable at all: `bpo_only` and a type
    with nothing listed cap instead, running fewer jobs rather than inventing a purchase.

    None = nothing is known about the print (a type with neither an owned copy nor a listing), which
    leaves the old uncapped behaviour exactly as it was.

    **Reactions have a formula, and a formula is an item too** — it locks into the reactor for the
    job, so one formula is one concurrent reaction however many reactor slots are free (a real
    2× Phoenix queue planned Axosomatic Neurolink Enhancer as 17 simultaneous jobs off the ONE
    formula that account holds). It differs from a blueprint copy in three ways that all matter:
    formulas cannot be copied, so runs-per-job never binds and this is purely a CONCURRENCY bound;
    they STACK, so the cap is how many items are held (20× Synth Mindflood is 20 jobs, not one); and
    they are DURABLE, so the plan never buys one — `acquisition_costs` already refuses to charge an
    original to a single build, and a formula is reused by every build after this one.

    **Unknown ownership must not serialise anything.** Blueprint scope is opt-in and per character —
    one production account has caches for 2 of its 14 characters — so a formula we cannot see means
    *we don't know*, never *they hold none*. No observation ⇒ no cap, exactly as today.
    """
    # Unknown ownership bites at TWO levels, and the per-type check below only catches one. `owned`
    # is a union over the characters that have a cached blueprint list, so on a partly-connected
    # account every count in it is a floor: prod account 1 has 2 of 14 characters cached and still
    # shows prints for 159 types, which the cap would take for the whole truth and serialise work the
    # builder can really run in parallel. Nothing is capped until the account's picture is complete
    # — EXCEPT a product whose holding the user DECLARED by hand, which is known on its own terms
    # and does not wait on a scope some other character never granted (`prints_known(tid)`).
    if not params.prints_known(tid):
        return None, False
    if activity != "manufacturing":
        own = (params.owned or {}).get(tid) or {}
        held = own.get("copies")
        n_owned = (len(held) if held else 1) if own else 0
        # Formulas the account keeps in a hangar rather than a personal blueprint list — a corp
        # container is invisible to /characters/{id}/blueprints/ entirely. Already de-duplicated
        # against `owned` (blueprints.stock_formula_prints), so this simply adds. It raises the
        # CONCURRENCY cap and nothing else: no ME/TE, no run coverage, because an asset row states
        # none of them.
        n_stock = int((params.stock_prints or {}).get(tid) or 0)
        if not n_owned and not n_stock:
            return None, False               # ownership unobserved — never cap on absent evidence
        return max(1, n_owned + n_stock), False
    copies = params.copies_for(tid, activity)
    acq = (params.bp_acquire or {}).get(tid) or {}
    buy_runs = int(acq.get("runs_per_copy") or 0)
    can_buy = acq.get("kind") == "bpc" and buy_runs > 0
    if not copies and not acq:
        return None, False                   # nothing to go on — don't invent a cap
    unlimited = any((c.get("runs") if c.get("runs") is not None else -1) < 0 for c in copies)
    owned_runs = 0 if unlimited else sum(max(0, int(c.get("runs") or 0)) for c in copies)
    short = 0 if unlimited else max(0, int(runs) - owned_runs)
    bought = math.ceil(short / buy_runs) if (short > 0 and can_buy) else 0
    # Owning nothing and buying nothing for runs still means ONE print: the original a `bpo_only`
    # type forces you to buy, or the single copy behind an unpriced listing.
    return max(1, len(copies) + bought), can_buy


def _jobs_on_copies(runs: int, n: int, copies: list[dict], fallback: tuple[float, float],
                    buy_runs: int | None) -> list[tuple[int, float, float]]:
    """[(runs, me, te)] — one entry per JOB, each running off ONE PRINT, best-researched first.

    `n` jobs are wanted, and the prints to run them on are the account's own copies (best first)
    followed by however many the plan buys. A job cannot span two prints, so the runs are dealt out
    one print at a time: each takes an even share (`_balanced`) or its proportional share of the
    capacity left, whichever is larger, and never more than it carries.

    That "or proportional" is what stops runs stranding. Dealing purely evenly gives a 5-run copy
    three runs beside a 1-run copy's one, leaves two runs with nowhere to go, and needs a SECOND job
    back on the first copy — which is a job the plan counted as concurrent and physically is not.
    Sizing by capacity lands the same work in two jobs on the two prints there actually are.

    Runs past everything owned are built off the copy the plan would buy, at that copy's research: a
    20-run batch against a 10-run ME10 copy is ten runs at ME10 and ten at whatever the market is
    selling, not twenty at ME10.
    """
    left = int(runs)
    if left <= 0:
        return []
    n = max(1, int(n))

    def bought() -> tuple[int | None, float, float]:
        return (int(buy_runs) if buy_runs else None, fallback[0], fallback[1])

    prints: list[tuple[int | None, float, float]] = []
    for c in copies:
        cr = c.get("runs")
        cap = None if (cr is None or int(cr) < 0) else max(0, int(cr))
        if cap == 0:                       # a spent copy is not a print you can install on
            continue
        prints.append((cap, c["me"], c["te"]))
    while len(prints) < n:                 # the rest of the wanted jobs run off bought copies
        prints.append(bought())

    out: list[tuple[int, float, float]] = []
    i = 0
    while left > 0:
        if i >= len(prints):
            # The prints ran out of runs before the batch did. That batch is already reported
            # `runs_short`; it needs more copies, and each of those is another print.
            prints.append(bought())
        cap, me, te = prints[i]
        rest = prints[i:]
        even = _balanced(left, len(rest))[0]
        finite = [c for c, _m, _t in rest if c is not None]
        prop = (math.ceil(left * cap / sum(finite))
                if cap is not None and len(finite) == len(rest) and sum(finite) > 0 else even)
        want = max(even, prop)
        take = max(1, min(left, want if cap is None else min(cap, want)))
        out.append((take, me, te))
        left -= take
        i += 1
    return out


# How far a job may overshoot its window to reach the next whole run.
#
# Runs are indivisible and rarely divide the pace evenly: four 2h 33m runs against a 5h 05m pace is
# 1.996 runs per job, and refusing that by 26 seconds leaves four jobs holding four slots. But the
# same rounding on a shorter job doubled it and pushed everything downstream — so the question is
# never "how much longer is this job", it is **"does this move the delivery"**.
#
# Both bounds have to hold. The first keeps any single job from ballooning; the second is the one
# that matters commercially: a builder quoting 8 days against a competitor's 14 cannot spend hours
# to save logins, while on a 14-day build a few minutes is nothing. Small enough that it can never
# be the difference between winning a contract and losing it, in either direction.
# A job holding ONE run can only grow by taking a second, and that is a 100% increase by
# definition. Every smaller allowance tried here — 5%, then a flat 20 minutes, then 2% of the
# makespan — was arithmetically incapable of merging a 1-run job however much slack it had, which
# is why an 18-slot component stayed at 18 slots through four attempts. So the per-job allowance is
# "you may double, if that is what reaching the next whole run costs". It is not the safety bound;
# _DELIVERY_OVERSHOOT below is, and it is the one that protects the quote.
#
# Measured on a real 206-hour Archon: 232 jobs -> 159 (a third of the slots back) for 32 minutes,
# +0.26%. It plateaus there — past 100% nothing more merges, because what is left is bounded by
# runs available, blueprint copy caps and genuine dependencies.
_PACE_OVERSHOOT = 1.0           # of the job's own window
_DELIVERY_OVERSHOOT = 0.02      # of the whole build's makespan
# ...and a floor under the first, because a percentage of a short window is seconds and nobody is
# served by that. A builder does not log in for fun: they log in to set everything going at once,
# and whether the jobs then land ten or twenty minutes apart is immaterial — what matters is that
# the slots are working while they are away, and that the ones they don't need are free for the
# next order. Twenty minutes is worth a slot; it is not worth anything to anyone waiting on a
# delivery, which is what the makespan bound above is there to guarantee.
_ALIGN_FLOOR = 20 * 60


def _packed_jobs(p: dict) -> int:
    """How many jobs this type needs to land inside its window.

    A type always has at least its OWN slack, before any consumer is considered: an uneven split
    finishes when the biggest chunk does, so every other job may carry that many runs too and land
    at the same moment. That was the whole of the first reported case — 8 jobs of 1 run beside 2 of
    2, all of the same thing, finishing 2h 32m and 5h 05m respectively.

    Work in RUNS PER JOB, never job count: runs are indivisible, so the question is how many of them
    fit the window, capped by what one blueprint copy may carry.
    """
    # `_align_cohorts` has already decided this one, having seen what it runs BESIDE — which is the
    # one thing this function cannot see from a single type's window.
    if p.get("aligned_jobs"):
        return p["aligned_jobs"]
    window = max(p.get("window", 0.0), p["wide_dur"])
    if window <= 0 or p["per_run"] <= 0:
        return p["n_wide"]
    per_job = max(1, min(p["cap"], int(window / p["per_run"] + 1e-9)))
    # One more run, when it lands within touching distance of the pace. Four 2h 33m runs against a
    # 5h 05m pace is 1.996 runs a job — refusing that by 26 seconds keeps four jobs in four slots,
    # and pushes whatever consumes them back by a minute and a half if we take it. A minute and a
    # half for two slots is the trade this feature exists to make.
    # 5% of the BINDING window — never of the pace when something needs this sooner. Reaching past a
    # real deadline by the whole of a run is not a sliver: a half-hour component with half an hour of
    # room became an hour-long one that way, and everything downstream of it moved. And whatever that
    # comes to, it may not cost a meaningful slice of the DELIVERY.
    slack = min(max(_ALIGN_FLOOR, window * _PACE_OVERSHOOT),
                (p.get("makespan") or window) * _DELIVERY_OVERSHOOT)
    allowed = window if p.get("no_consumer") else window + slack
    # A ceiling is a ceiling, so the "+1 run" allowance may not reach past it. It cannot make the job
    # shorter than the window already forced (the max below), which matters where the ceiling is
    # unreachable: there the window has already fallen back to `wide_dur` and this must not fight it.
    if p.get("job_ceiling"):
        allowed = min(allowed, max(p["job_ceiling"], window))
    if per_job < p["cap"] and (per_job + 1) * p["per_run"] <= allowed + 1e-9:
        per_job += 1
    return max(1, min(p["n_wide"], math.ceil(p["runs"] / per_job)))


def _packed_duration(p: dict) -> float:
    """How long this type will take once packed — the longest of its jobs, runs being whole."""
    return math.ceil(p["runs"] / _packed_jobs(p)) * p["per_run"]


def _tightest(p: dict) -> float:
    """The tighter of the two "never longer than" bounds — the plan's pace, and the account's own
    ceiling on one reaction job. They are the same kind of bound, which is why the ceiling rides in
    the same min() as `pace_cap` rather than in a splitting path of its own. `hard_window` is
    deliberately NOT in here: a deadline is a different promise, and it is compared against this.
    With no ceiling set this is exactly `pace_cap`, so every plan reads as it always did."""
    pace = p.get("pace_cap", 0.0)
    ceiling = p.get("job_ceiling")
    return min(pace, ceiling) if ceiling else pace


def _align_cohorts(plan: dict, start: dict) -> None:
    """Lift every job to the longest one already running BESIDE it, so a wave lands in one go.

    A window tells a type how long it may take before it holds something up. It cannot tell it when
    to LAND, and those are different questions — which is the whole of why tuning the allowance
    never worked. Measured on the real Archon: sweeping `_DELIVERY_OVERSHOOT` from 2% to 100% moved
    nothing whatsoever, because a job could only ever take ONE run past its window and Oxy-Organic
    Solvents needed three. Widening it enough to close that gap grew a *different* job to 15h 18m,
    past the 10h 12m the wave was landing at — more slack, worse alignment. An allowance grows a
    job; only a target lands it.

    So the target is the longest job the cohort already has, and the cohort is everything that
    starts at the same moment. No new pace is set — same principle as `pace_cap`, scoped to what a
    builder is actually looking at when they log in rather than to the whole plan. A type that
    already finishes at the cohort's pace is untouched; one finishing early is given the runs to
    land with it, in fewer slots.

    A **deliverable is exempt** for the same reason it is exempt from the overshoot: the alignment
    buys slots by finishing components later, and a finished product has no later to give.

    This can genuinely overrun a consumer's start, so it is NOT free — but the bound that decides
    whether it was worth it lives in `plan_queue`, measured on the scheduled makespan, because that
    is the number quoted. Enforcing it here instead was tried twice and was wrong both times:

    - **Per type it cannot work at all.** Oxy-Organic's own window is 2h 33m against a 4h 08m
      allowance, so any per-type bound rejects the 10h 12m job — which is the merge this exists for,
      and which costs the delivered plan nothing.
    - **Per plan, on THIS model, it is too pessimistic.** Re-timing here ignores slot contention (by
      design — see the note above the backward pass, it is the safe direction for slack). On the
      real Archon the model read 211h where the schedule delivered 210.46h, and the give-back
      spent that phantom difference on exactly the Oxy-Organic merge, for four fewer slots and not
      one minute of delivery.

    So: align, then let the caller check the real number and drop the whole thing if it did not pay.
    """
    cohorts: dict[float, list[int]] = defaultdict(list)
    for tid, p in plan.items():
        if not p.get("no_consumer") and p["per_run"] > 0:
            cohorts[round(start.get(tid, 0.0), 3)].append(tid)

    want: dict[int, int] = {}
    for members in cohorts.values():
        target = max(_packed_duration(plan[t]) for t in members)
        for tid in members:
            p = plan[tid]
            # Alignment LENGTHENS a job to land with its cohort, which is precisely what a job-length
            # ceiling forbids. So the target is clipped to the ceiling for a type that has one: it
            # then asks for at least as many jobs as the type already has, the `n <` guard below
            # declines, and the type simply keeps its own landing. Without a ceiling this is the
            # cohort target unchanged.
            tgt = min(target, p["job_ceiling"]) if p.get("job_ceiling") else target
            per_job = max(1, min(p["cap"], int(tgt / p["per_run"] + 1e-9)))
            n = max(1, min(p["n_wide"], math.ceil(p["runs"] / per_job)))
            if n < _packed_jobs(p):
                want[tid] = n
    for tid, n in want.items():
        plan[tid]["aligned_jobs"] = n


def build_tasks(agg: dict, mfg: dict, rx: dict, params: BuildParams,
                pools: dict[str, int] | None = None,
                depths: dict[int, int] | None = None,
                deps: dict[int, set[int]] | None = None,
                align: bool = True,
                parallel_copies: dict[int, int] | None = None,
                print_gaps: dict[int, dict] | None = None,
                plan_out: dict | None = None,
                start_out: dict | None = None,
                align_hint: dict[int, int] | None = None) -> tuple[list[Task], dict]:
    """One or more Task instances per built type. A type's runs are split so they can run in
    PARALLEL across the pool's slots — up to `pool_size` concurrent jobs — while still respecting
    the per-BPC run cap (`max_runs`). This is what lets a big reaction (thousands of runs, no BPC
    cap → otherwise one multi-month job) actually spread across your reaction slots the way you'd
    run it in-game. Returns (tasks, tasks_by_type).

    **Slots are only spent where they buy time.** Splitting every type as wide as the pool allows
    is wasteful: a stage finishes when its SLOWEST component does, so anything that would finish
    earlier can run in fewer slots and land at the same moment. Two runs of an hour each, alongside
    a two-hour job that can't be split, is one slot for two hours — not two slots for one — and the
    stage still completes at the two-hour mark. The freed slots are the point: they're what lets a
    builder start the next order (or a batch of something else entirely) instead of watching jobs
    idle out.

    `depths` groups types into the stages the plan actually works through. Each stage's pace is set
    by its own slowest member under full parallelism; every other type in it is then given the
    FEWEST jobs that still finish by then. The per-BPC cap and "at most one job per run" still bind,
    and no type is ever given fewer than one job. Without `depths` the old maximal split is used, so
    callers that don't have the dependency graph to hand are unaffected.

    **A type never runs more jobs at once than it has PRINTS** (`_print_limits`). Slots and runs are
    not the only thing a job needs: it needs a blueprint, and that blueprint is LOCKED for the
    duration of the job. Where filling the free slots would take more prints than the account holds
    and copies ARE listed, the plan buys them — and reports them separately through `parallel_copies`
    ({type_id: copies}), because a copy bought to fill a SLOT is a different purchase from one bought
    because the RUNS are short, and on a capital hull that difference is billions the builder did not
    ask to spend. Where copies cannot be bought (`bpo_only`, nothing listed) the cap simply stands
    and the type runs fewer, longer jobs — and what holding more prints would be worth is reported
    through `print_gaps` ({type_id: {held, jobs, could_run, extra, hours, hours_if_held}}) instead.
    That is the whole answer for a REACTION: a formula is durable and reused by every later build,
    so the plan says what another one would save and never spends the builder's ISK on it.

    **A REACTION job may also be held to a MAXIMUM LENGTH** (`params.max_reaction_job_hours`, 0 =
    off). Reactions have no per-job run cap, so the compaction above will happily park 5,000 runs in
    one reactor for weeks. The account's ceiling is the same KIND of bound as `pace_cap` — a "never
    longer than", not a target — so it rides in the same window and this machinery does the
    splitting; there is deliberately no second path. It can only ever make jobs SHORTER, a consumer's
    deadline still outranks it, and it gets no say on concurrency: the split stays bounded by
    `n_wide`, which already holds the slot pool and the formula cap. Where that is not enough the
    ceiling is honoured as far as it goes and the miss is reported (`why.ceiling_met`). Manufacturing
    is untouched on purpose — splitting a manufacturing batch spends blueprint COPIES, which cost
    ISK, while a formula is durable and reused by every later build.

    **Aligning ACROSS several calls** (`plan_out` / `start_out` / `align_hint`). Cohort alignment
    only ever sees the types in ONE call, which is right for the aggregated queue — it is one plan —
    and wrong for per-order planning, where the same login covers every order's jobs. So a caller
    planning orders separately can run each of them once with `align=False`, collect their packing
    state (`plan_out`) and earliest starts (`start_out`), align the union itself, and replay each
    order with the answer in `align_hint` ({type_id: jobs}). Nothing else changes: the hint lands
    exactly where `_align_cohorts` would have written it, so a hinted replay of a single order is
    byte-identical to the un-hinted call.
    """
    pools = pools or {}
    tasks: list[Task] = []
    by_type: dict[int, list[Task]] = {}

    # Pass 1: the shape of each type's work, and the widest split that is legal for it.
    plan: dict[int, dict] = {}
    for tid, info in agg.items():
        if not info["build"] or info["runs"] <= 0:
            continue
        recipe = mfg.get(tid) or rx.get(tid)
        activity = info["activity"]
        max_runs = mfg[tid]["max_runs"] if tid in mfg else 0
        R = info["runs"]
        # The batch-level ME/TE — what the WINDOWS are measured with. The jobs themselves each take
        # the research of the copy they run on (see `_jobs_on_copies` below), which legitimately
        # differ inside one batch; the packing has to work off one figure, and the runs-weighted one
        # is the batch's own average rather than its best moment.
        _me, te = params.me_te_for(tid, activity, R)
        _mm, st = params.struct_mults_for(tid, activity)
        skill = params.mfg_skill_time_mult if activity == "manufacturing" else params.rx_skill_time_mult
        base_run = recipe["base_time"] * st * skill
        per_run = base_run * (1 - te / 100.0)
        P = max(1, pools.get(activity, 1))
        cap = max_runs if max_runs else R
        # A manufacturing job also cannot carry more runs than the BLUEPRINT COPY it runs off. The
        # SDE cap above is the blueprint type's per-job limit; this is one copy's runs, which is
        # usually far smaller and is what actually binds in practice. It matters specifically because
        # the packing below makes jobs LONGER: a 35-run batch is happy as one job when the plan has
        # the slack for it, and impossible if your copies carry 10 runs each. Reactions have no
        # blueprint and are untouched.
        copies, copy_cap, buy_runs = _copy_limits(params, tid, activity, R)
        if copy_cap is not None:
            cap = min(cap, copy_cap)
        cap = max(1, cap)
        # (a) fill up to P slots concurrently, (b) never exceed the per-job run cap — whichever
        # forces MORE jobs. This is the widest split; the floor is what the cap alone demands.
        n_floor = math.ceil(R / cap)
        n_wide = max(min(P, R), n_floor)
        # ...and (c) a job needs a PRINT, which is locked while it runs. One copy is one job at a
        # time whatever the pool has free. When more prints can't be bought the cap simply binds; the
        # run floor still wins, because splitting below it would emit a job no copy can carry (that
        # batch is already reported `runs_short` — it is short of runs, not of parallelism).
        prints, can_buy_prints = _print_limits(params, tid, activity, R)
        n_free = n_wide
        if prints is not None and not can_buy_prints:
            n_wide = max(n_floor, min(n_wide, prints))
        # The longest ONE job of this type may run, when the account has asked for a ceiling.
        # REACTIONS ONLY: splitting a manufacturing batch spends blueprint copies, which cost ISK,
        # whereas a formula is durable and reused — that asymmetry is why only this half exists.
        # None everywhere when the setting is unset or the flag is off, and every expression below
        # that reads it is a no-op on None, so the plan is byte-for-byte the one that shipped.
        job_ceiling = (params.max_reaction_job_hours * 3600.0
                       if activity == "reaction" and getattr(params, "max_reaction_job_hours", 0.0) > 0
                       else None)
        plan[tid] = {"activity": activity, "per_run": per_run, "base_run": base_run,
                     "copies": copies, "prints": prints, "can_buy_prints": can_buy_prints,
                     "job_ceiling": job_ceiling,
                     # What the split would have been with prints to spare — the difference is what
                     # holding more of them would buy, which is worth SAYING even where it isn't
                     # worth spending (a formula is durable; the plan reports, never buys).
                     "n_free": n_free,
                     # What runs past the owned copies are built at. With no owned copies at all
                     # this is simply the batch figure — so a plan with nothing owned, or with a
                     # user override, comes out exactly as it did before any of this.
                     "buy_me_te": (params.buy_me_te_for(tid)
                                   if activity == "manufacturing" and copies else (_me, te)),
                     "buy_runs": buy_runs,
                     "runs": R, "cap": cap,
                     "n_wide": n_wide, "n_min": math.ceil(R / cap),
                     # A split of R runs into n jobs finishes when the LONGEST job does, and runs
                     # are whole: ceil(R/n) of them land in the biggest chunk. Using the average
                     # (work/n) here is what hid the case this was reported for — 29 runs over 29
                     # slots is not 29 equal jobs, it is a few 2-run jobs setting the pace while
                     # the 1-run ones finish in half the time and their slots sit idle.
                     "wide_dur": math.ceil(R / n_wide) * per_run,
                     "work": R * per_run, "stage": (depths or {}).get(tid, 0)}

    # Pass 2: how long each type may take without holding anything up — its SLACK.
    #
    # The deadline for a component is when the job that consumes it can actually start, and nothing
    # else. An earlier version paced each type against its stage-mates in the same pool, which is a
    # crude proxy for the same idea and misses the cases that matter: a type alone at its stage
    # paces against itself and stays fully split, and two types that feed the same job but sit at
    # different depths never see each other at all. Real example that exposed it — one component
    # taking 5h 05m across 8 jobs beside another taking 2h 32m across 9, both feeding the same work:
    # the second could run 2 runs per job (5h 04m) and free 4 slots without moving anything.
    #
    # So: a forward pass for the earliest each type can start, then each type's deadline is the
    # earliest start of whatever consumes it (the final products get the makespan). Anything with
    # room to spare runs in fewer, longer jobs and lands at the same moment it would have anyway.
    # The freed slots are the point — they're what lets a builder start other work.
    #
    # Makespan-preserving by construction: no earliest-start ever moves, because every type still
    # finishes by the time its consumer needed it. Slot contention is ignored here, which is the
    # safe direction — consolidating only ever REDUCES the number of jobs competing for slots, and
    # a contended schedule's real start times are later than this model's, so the slack is real.
    if depths is not None and deps is not None:
        order = sorted(plan, key=lambda t: -plan[t]["stage"])      # inputs first
        consumers: dict[int, set[int]] = {}
        for tid, ds in (deps or {}).items():
            for d in ds:
                consumers.setdefault(d, set()).add(tid)

        start: dict[int, float] = {}
        finish: dict[int, float] = {}
        for tid in order:
            p = plan[tid]
            st = max([finish.get(d, 0.0) for d in (deps.get(tid) or ()) if d in plan] or [0.0])
            start[tid] = st
            finish[tid] = st + p["wide_dur"]
        makespan = max(finish.values()) if finish else 0.0

        # Backward pass, CONSUMERS FIRST, and it has to be in that order: a component's deadline is
        # when the job eating it must start, and that job's own start is only known once IT has been
        # stretched. Deciding roots first and walking down is what lets slack propagate — otherwise
        # a component whose consumer is itself off the critical path never inherits any, which is
        # how a 2h 32m job stayed 2h 32m in a plan whose critical path was four times as long, and
        # why the builder ended up with three separate moments to log in at instead of one.
        # NEVER make a job longer than the longest job the plan already has. Slack says a component
        # could take the whole critical path; taking it is a different question. Stretching four
        # 2h 33m runs into one 10h 11m job is "free" only in a model with unlimited slots and no
        # interest in when that item is finished — in the real plan it puts the thing seven and a
        # half hours further away and holds one slot for the whole of it. Compaction is for filling
        # up to the pace the plan already runs at, not for setting a new one.
        pace_cap = max((p["wide_dur"] for p in plan.values()), default=0.0)

        # Consumers strictly before their inputs — a REAL topological order, not depth order.
        # `_depths` records the deepest level a type is reached at, so a type used both as a direct
        # input to the product and again further down gets the deeper number, and its own consumer
        # can end up sorted AFTER it. When that happened the consumer had no `latest_start` yet, the
        # input fell through to "no slack at all", and a 0.7h job sat next to its 4.7h siblings for
        # no reason anyone could see from the outside. Kahn's algorithm on the consumer graph, with
        # any cycle remnant appended so nothing is silently dropped.
        pending = {t: {c for c in (consumers.get(t) or ()) if c in plan} for t in plan}
        order2, done_set = [], set()
        while True:
            ready = [t for t, cs in pending.items() if t not in done_set and cs <= done_set]
            if not ready:
                break
            ready.sort(key=lambda t: plan[t]["stage"])      # stable, and nice for readability
            order2.extend(ready)
            done_set.update(ready)
        order2.extend(t for t in plan if t not in done_set)

        latest_start: dict[int, float] = {}
        for tid in order2:
            p = plan[tid]
            # A type with NO consumer is a deliverable and answers to ITSELF, never to the makespan:
            # pacing a finished product against the slowest thing in the queue trades the one number
            # a customer feels for slots nobody asked to free.
            cons = [(latest_start[c], c) for c in (consumers.get(tid) or ()) if c in latest_start]
            deadline, binder = min(cons) if cons else (finish[tid], None)
            p["needed_by"] = binder
            # Nothing is waiting on this, so it IS the delivery. It may still be packed to its own
            # natural length (an uneven split), but it may not be stretched a minute past that: the
            # overshoot allowance buys slots by finishing components later, and a finished product
            # has no later to give.
            p["no_consumer"] = not cons
            # Bounded by the plan's existing longest job, so compaction can close a gap but never
            # open a new one. `hard_window` keeps the dependency deadline on the side: the pace may
            # be overshot by a hair to reach a whole run, a consumer's start may not.
            p["hard_window"] = max(0.0, deadline - start[tid])
            p["pace_cap"] = pace_cap
            p["makespan"] = makespan
            p["window"] = min(p["hard_window"], pace_cap)
            # …and the account's own ceiling on one reaction job, which is the same KIND of bound as
            # `pace_cap` — a "never longer than", not a target — so it belongs in the same min().
            # It can only ever shrink the window, so it can only ever make MORE, SHORTER jobs; it
            # never moves a delivery later and it cannot beat `hard_window`, because a smaller window
            # still lands inside a deadline that was already met. What it CANNOT do is buy
            # concurrency: `_packed_jobs` never returns more than `n_wide`, which already carries the
            # slot pool and the formula cap, so an unreachable ceiling is honoured as far as the
            # slots and formulas allow and then reported (`ceiling_met` below) rather than missed in
            # silence.
            if p.get("job_ceiling"):
                p["window"] = min(p["window"], p["job_ceiling"])
            # What this type will actually take once packed into that window — decided here so its
            # own inputs can be given the room it leaves behind.
            dur = _packed_duration(p)
            latest_start[tid] = deadline - dur

        # Last, because it needs every type's natural length decided first: what each one runs
        # beside is only knowable once they have all been packed. `align=False` is how the caller
        # asks for the same plan without it, to price what it cost.
        if align_hint is not None:
            # Somebody else has already seen the cohorts — across every order, which this call
            # cannot — and decided. Applied exactly where the local answer would have gone.
            for tid, n in align_hint.items():
                if tid in plan:
                    plan[tid]["aligned_jobs"] = n
        elif align:
            _align_cohorts(plan, start)
        if start_out is not None:
            start_out.update(start)
    if plan_out is not None:
        plan_out.update(plan)

    for tid, p in plan.items():
        n = _packed_jobs(p)
        # Prints bought purely to run these jobs side by side. Decided HERE, after packing, and not
        # off the widest split: the packed count is already the fewest jobs that land inside the
        # window, so every print this asks for is one that genuinely buys time — the same rule the
        # rest of this module works to. Nothing is bought where nothing is listed (`can_buy_prints`).
        if parallel_copies is not None and p.get("can_buy_prints") and p.get("prints") is not None:
            extra = max(0, n - p["prints"])
            if extra:
                parallel_copies[tid] = extra
        # ...and where prints are what's short and buying them is NOT the plan's call — a reaction
        # formula (durable, reused by every future build) or a blueprint with no copies on offer —
        # say what holding more would be worth instead of quietly running slower.
        if (print_gaps is not None and p.get("prints") is not None and not p.get("can_buy_prints")
                and p.get("n_free", 0) > p["prints"]):
            free_jobs = max(1, p["n_free"])
            print_gaps[tid] = {
                "activity": p["activity"], "held": p["prints"], "jobs": n,
                "could_run": free_jobs, "extra": free_jobs - p["prints"],
                "hours": round(math.ceil(p["runs"] / n) * p["per_run"] / 3600.0, 2),
                "hours_if_held": round(math.ceil(p["runs"] / free_jobs) * p["per_run"] / 3600.0, 2),
            }
        # The account's ceiling on one reaction job, and whether the plan could actually keep to it.
        # A ceiling cannot buy slots or formulas, so where it needs more concurrency than the pool
        # or the formula cap can supply it is honoured as far as it goes and the shortfall SAID —
        # a target quietly missed is worse than one never offered. `ceiling` is absent entirely for
        # everything else, which is every job in a plan without the setting.
        ceiling = p.get("job_ceiling")
        packed_h = math.ceil(p["runs"] / n) * p["per_run"] / 3600.0
        why = {
            "runs_per_job": math.ceil(p["runs"] / n),
            "runs": p["runs"], "jobs": n,
            "per_run_h": round(p["per_run"] / 3600.0, 3),
            "hard_h": (round(p["hard_window"] / 3600.0, 2) if p.get("hard_window") is not None else None),
            "window_h": round(max(p.get("window", 0.0), p["wide_dur"]) / 3600.0, 2),
            "pace_h": round(p.get("pace_cap", 0.0) / 3600.0, 2),
            "own_h": round(p["wide_dur"] / 3600.0, 2),
            "ceiling_h": (round(ceiling / 3600.0, 2) if ceiling else None),
            "ceiling_met": (packed_h <= ceiling / 3600.0 + 1e-9) if ceiling else None,
            # What stopped it being longer. "consumer" means something needs it sooner than the
            # plan's pace, which is the answer that surprises people; "job_length" means the account
            # said no reaction job may run longer than this, and it bit before the pace did. The
            # deadline still outranks both — a ceiling makes a job shorter, it never excuses one
            # landing late.
            "bound_by": ("aligned" if p.get("aligned_jobs")
                         else "consumer" if p.get("hard_window") is not None
                         and p.get("hard_window", 0.0) < _tightest(p) - 1e-9
                         else "job_length" if ceiling and ceiling < p.get("pace_cap", 0.0) - 1e-9
                         else "pace" if p.get("pace_cap") else "own"),
            "needed_by": p.get("needed_by"),
            "needed_by_name": None,
        }
        # Each job runs off ONE copy and carries that copy's research — best copies first, so the
        # first jobs installed are the well-researched ones. This can add jobs the packing didn't
        # ask for (a chunk longer than the copy it lands on has to be split), which is the honest
        # answer: the alternative is a job that cannot be installed.
        jobs = _jobs_on_copies(p["runs"], n, p["copies"], p["buy_me_te"], p["buy_runs"])
        for i, (r, me_i, te_i) in enumerate(jobs):
            t = Task(f"{tid}-{i}", tid, p["activity"], r,
                     r * p["base_run"] * (1 - te_i / 100.0))
            t.why = why
            if p["activity"] == "manufacturing":
                t.me, t.te = me_i, te_i
            tasks.append(t)
            by_type.setdefault(tid, []).append(t)
    return tasks, by_type


def _built_deps(agg: dict, mfg: dict, rx: dict) -> dict[int, set[int]]:
    """For each built type, the set of its inputs that are ALSO built (bought inputs are available
    at t=0, so they're not scheduling dependencies)."""
    deps: dict[int, set[int]] = {}
    for tid, info in agg.items():
        if not info["build"]:
            continue
        recipe = mfg.get(tid) or rx.get(tid)
        deps[tid] = {inp["type_id"] for inp in recipe["inputs"]
                     if agg.get(inp["type_id"], {}).get("build")}
    return deps


def _critical_priority(agg: dict, deps: dict, mfg: dict, rx: dict, params: BuildParams) -> dict[int, float]:
    """crit_to_end(type) = own duration + longest crit of any consumer — the classic list-scheduler
    priority. Higher = more urgent (on a longer path to a finished product)."""
    consumers: dict[int, set[int]] = defaultdict(set)
    for tid, dset in deps.items():
        for d in dset:
            consumers[d].add(tid)

    def type_dur(tid: int) -> float:
        info = agg[tid]
        recipe = mfg.get(tid) or rx.get(tid)
        _me, te = params.me_te_for(tid, info["activity"], info["runs"])
        _mm, st = params.struct_mults_for(tid, info["activity"])
        skill = params.mfg_skill_time_mult if info["activity"] == "manufacturing" else params.rx_skill_time_mult
        return info["runs"] * recipe["base_time"] * (1 - te / 100.0) * st * skill

    memo: dict[int, float] = {}

    def crit(tid: int, stack: frozenset) -> float:
        if tid in memo:
            return memo[tid]
        if tid in stack:
            return 0.0
        best = 0.0
        for c in consumers.get(tid, ()):
            best = max(best, crit(c, stack | {tid}))
        memo[tid] = type_dur(tid) + best
        return memo[tid]

    return {tid: crit(tid, frozenset()) for tid in deps}


def order_ranks(targets: list[tuple[int, int]], mfg: dict, rx: dict) -> dict[int, int]:
    """type_id -> rank of the EARLIEST queued order that needs it (0 = first in line).

    Demand is aggregated by type, so a component shared by orders 1 and 3 is one batch; it inherits
    rank 0 because order 1 is waiting on it. That's the point: shared work keeps the efficiency of a
    single batch while still being scheduled as urgently as the earliest order that needs it.
    """
    rank: dict[int, int] = {}
    for i, (tid, _qty) in enumerate(targets):
        for t in collect_reachable(tid, mfg, rx):
            if t not in rank or i < rank[t]:
                rank[t] = i
    return rank


def _fifo_priority(crit: dict[int, float], rank: dict[int, int]) -> dict[int, tuple]:
    """Scheduling priority: first-in-line WINS a contested slot, critical path breaks ties.

    Critical path alone minimises total makespan but is order-blind — when slots are scarce a later
    order's long chain can take the slot the first order's next stage was waiting for, which is
    exactly backwards for someone filling customer orders. Ranking first means order 1 is never held
    behind order 2, while order 2 still gets every slot order 1 isn't using.
    """
    return {tid: (-rank.get(tid, 0), crit.get(tid, 0.0)) for tid in crit}


def schedule(tasks: list[Task], by_type: dict, deps: dict, pools: dict[str, int],
             priority: dict[int, float]) -> dict:
    """Event-driven resource-constrained list scheduler. Fills each pool's free slots with the
    highest-priority ready tasks, advancing time to the next job completion when nothing more can
    start. Returns makespan, per-task placements, and waves (tasks grouped by start time)."""
    free = dict(pools)
    completed: set[int] = set()
    running: list[Task] = []
    started: set[str] = set()
    now = 0.0
    remaining = len(tasks)

    while remaining > 0:
        # Start every ready task that fits a free slot in its pool, most-critical first.
        ready = sorted(
            (t for t in tasks if t.task_id not in started
             and all(d in completed for d in deps.get(t.sched_key(), ()))),
            key=lambda t: priority.get(t.sched_key(), (0, 0.0)), reverse=True,
        )
        started_any = False
        for t in ready:
            if free.get(t.activity, 0) > 0:
                t.slot = pools[t.activity] - free[t.activity]
                t.start = now
                t.end = now + t.duration
                free[t.activity] -= 1
                started.add(t.task_id)
                running.append(t)
                remaining -= 1
                started_any = True
        if started_any:
            # Newly started tasks don't unlock anything until they finish; fall through to advance.
            pass
        if not running:
            if not started_any:
                break  # nothing running and nothing startable → unschedulable remainder
            continue
        # Advance to the next completion(s), free those slots, mark newly-complete types.
        next_end = min(t.end for t in running)
        now = next_end
        for t in [x for x in running if x.end == next_end]:
            free[t.activity] += 1
            running.remove(t)
        for tid in by_type:
            if tid not in completed and all(x.end and x.end <= now for x in by_type[tid]):
                completed.add(tid)

    makespan = max((t.end for t in tasks if t.end), default=0.0)
    waves: dict[float, list[Task]] = defaultdict(list)
    for t in tasks:
        if t.task_id in started:
            waves[t.start].append(t)
    wave_list = [
        {"start_hours": round(s / 3600.0, 2),
         "tasks": [{"type_id": t.type_id, "activity": t.activity, "runs": t.runs,
                    "duration_hours": round(t.duration / 3600.0, 2), "slot": t.slot,
                    "why": t.why,
                    # The copy THIS job runs on. Two jobs of one type can differ, so the type-level
                    # figure on the requirement can't answer "why is that one longer".
                    **({"me": t.me, "te": t.te} if t.me is not None else {}),
                    # WHOSE job this is. Only ever set when orders are planned apart — that is the
                    # only case where the answer exists, and it is the whole point of the split:
                    # with three builds in flight, "install 40 runs of Capital Armor Plates" is not
                    # an instruction until you know which container they belong to.
                    **({"order_id": t.order_id} if t.order_id is not None else {})}
                   for t in sorted(ts, key=lambda t: t.type_id)]}
        for s, ts in sorted(waves.items())
    ]
    return {
        "makespan_hours": round(makespan / 3600.0, 2),
        "waves": wave_list,
        "unscheduled": [t.task_id for t in tasks if t.task_id not in started],
    }


# ── Orchestrator + endpoint ───────────────────────────────────────────────────────────────────

def _finish_of(tasks: list, type_id: int) -> float:
    """Hours until the last job producing `type_id` completes — i.e. that order is deliverable."""
    ends = [t.end for t in tasks if t.type_id == type_id and t.end is not None]
    return round(max(ends) / 3600.0, 2) if ends else 0.0


def _sites_used(agg: dict, params: BuildParams) -> list[dict]:
    """The distinct structures this plan installs jobs in, with how many build steps each takes —
    the one-line answer to "where is this build actually happening"."""
    used: dict[str, dict] = {}
    for tid, info in agg.items():
        if not info.get("build") or info.get("runs", 0) <= 0:
            continue
        site = params.site_for(tid, info["activity"])
        if not site:
            continue
        row = used.setdefault(site["key"], {"key": site["key"], "name": site["name"],
                                            "system_id": site.get("system_id"), "steps": 0,
                                            "pinned": False})
        row["steps"] += 1
        # Whether anything here is here BECAUSE the user said so, rather than because it scored best.
        row["pinned"] = row["pinned"] or bool(site.get("pinned"))
    return sorted(used.values(), key=lambda r: -r["steps"])


def _job_length_limits(tasks: list, names: dict[int, str]) -> list[dict]:
    """Reaction steps the account's job-length ceiling could NOT be honoured on, one row per type.

    Derived from the `why` the packer already wrote rather than through a fourth out-parameter: the
    ceiling has no separate splitting path — it rides in the window with `pace_cap` — so there is no
    separate state to collect, only the answer to read back. Empty whenever no ceiling is set, and
    empty when every reaction kept to it, which is the case a plan should say nothing about.

    A ceiling cannot make slots and cannot make formulas: `_packed_jobs` never returns more jobs
    than `n_wide`, which already carries the reactor pool and the formula cap. So it is honoured as
    far as the concurrency goes and the remainder is reported here — the plan states that it fell
    short rather than presenting the longer job as if it were what was asked for.
    """
    rows: dict[int, dict] = {}
    for t in tasks:
        why = t.why if hasattr(t, "why") else (t or {}).get("why")
        if not why or not why.get("ceiling_h") or why.get("ceiling_met"):
            continue
        tid = t.type_id if hasattr(t, "type_id") else t["type_id"]
        rows[tid] = {"type_id": tid, "name": names.get(tid, str(tid)),
                     "ceiling_h": why["ceiling_h"],
                     # The longest job as PACKED — runs are whole, so this is the honest figure and
                     # not the window it was measured against.
                     "hours": round(why["runs_per_job"] * why["per_run_h"], 2),
                     "jobs": why["jobs"], "runs_per_job": why["runs_per_job"]}
    return sorted(rows.values(), key=lambda r: -(r["hours"] - r["ceiling_h"]))


def plan_queue(targets: list[tuple[int, int]], mfg: dict, rx: dict, prices: dict, adjusted: dict,
               params: BuildParams, names: dict[int, str], pools: dict[str, int],
               on_hand: dict[int, float] | None = None) -> dict:
    """End-to-end queue plan: aggregate demand across all targets, schedule the jobs, and roll up
    cost + time metrics + a combined priced shopping list."""
    memo, unit = resolve_unit_costs(mfg, rx, prices, adjusted, params)
    for tid, _ in targets:
        unit(tid, frozenset())

    agg = aggregate_demand(targets, memo, mfg, rx, params, on_hand, pools)
    # Pass the stage depths so a type is only split as wide as its stage's pace requires — see
    # build_tasks. The same `_depths` the demand pass already walked.
    deps = _built_deps(agg, mfg, rx)
    # Deps as well as depths: a type's real deadline is when the job consuming it can start, which
    # is what decides how many slots it needs — see build_tasks.
    parallel: dict[int, int] = {}
    gaps: dict[int, dict] = {}
    tasks, by_type = build_tasks(agg, mfg, rx, params, pools,
                                 depths=_depths([t for t, _ in targets], mfg, rx), deps=deps,
                                 parallel_copies=parallel, print_gaps=gaps)
    crit = _critical_priority(agg, deps, mfg, rx, params)
    ranks = order_ranks(targets, mfg, rx)
    prio = _fifo_priority(crit, ranks)
    sched = schedule(tasks, by_type, deps, pools, prio)

    # Did aligning the wave cost the delivery more than it is allowed to? This is the ONLY place
    # that can answer it, because it is the only place holding the scheduled makespan — the number
    # actually quoted. `_align_cohorts` deliberately does not try: its own model has no slot
    # contention, reads the plan as longer than it is, and gave back the merges it exists to make.
    # Two extra passes over prepared data, and only when the alignment changed something.
    if any(t.why and t.why.get("bound_by") == "aligned" for t in tasks):
        plain_parallel: dict[int, int] = {}
        plain_gaps: dict[int, dict] = {}
        plain_tasks, plain_by = build_tasks(agg, mfg, rx, params, pools,
                                            depths=_depths([t for t, _ in targets], mfg, rx),
                                            deps=deps, align=False, parallel_copies=plain_parallel,
                                            print_gaps=plain_gaps)
        plain = schedule(plain_tasks, plain_by, deps, pools, prio)
        if sched["makespan_hours"] > plain["makespan_hours"] * (1 + _DELIVERY_OVERSHOOT):
            tasks, by_type, sched = plain_tasks, plain_by, plain
            parallel, gaps = plain_parallel, plain_gaps

    for w in sched["waves"]:                       # enrich wave tasks with readable names
        for t in w["tasks"]:
            t["name"] = names.get(t["type_id"], str(t["type_id"]))
            # WHERE this job is installed. The checklist is an instruction, and "install 40 runs of
            # Capital Armor Plates" is not one when you run three structures with different rigs.
            site = params.site_for(t["type_id"], t["activity"])
            if site:
                t["site"] = site["name"]
                t["site_key"] = site["key"]
                # A pinned job says so. "I chose this building" and "the tool worked it out" are
                # different facts about the same line, and only one of them is worth arguing with.
                if site.get("pinned"):
                    t["site_pinned"] = site["pinned"]
            # Name whatever set this job's length, so the UI can say "held to 2h 32m because X
            # needs it then" rather than leaving the reader to infer it.
            if t.get("why") and t["why"].get("needed_by") is not None:
                t["why"]["needed_by_name"] = names.get(t["why"]["needed_by"],
                                                       str(t["why"]["needed_by"]))

    # Cost roll-up from the aggregated demand (single batch per type — the honest, shared-batch cost).
    materials_cost = 0.0
    job_cost = 0.0
    shopping = []
    for tid, info in agg.items():
        if info["build"]:
            recipe = mfg.get(tid) or rx.get(tid)
            eiv = sum(inp["quantity"] * info["runs"] * adjusted.get(inp["type_id"], 0.0)
                      for inp in recipe["inputs"])
            job_cost += eiv * params.job_fee_rate(tid, info["activity"])
        elif info["gross"] > 0:
            price = (prices.get(tid) or {}).get("sell_price")
            line = (price or 0.0) * info["gross"]
            materials_cost += line
            shopping.append({
                "type_id": tid, "name": names.get(tid, str(tid)), "qty": info["gross"],
                "unit_price": price, "source": (prices.get(tid) or {}).get("source"),
                "line_cost": line if price else None,
                "bought_for_speed": info.get("bought_for_speed", False),
                "bought_marginal": info.get("bought_marginal", False),
                "marginal_saving": info.get("marginal_saving"),
                "bought_no_blueprint": info.get("bought_no_blueprint", False),
                # On the list because the user said always buy this, not because building lost on
                # cost — a standing rule has to be visible where it takes effect, or the plan looks
                # like it just got the make-or-buy call wrong.
                "blacklisted": tid in params.never_build_ids,
                # Same reasoning one rung up: bought because this account doesn't run that kind of
                # reaction, not because building lost on cost.
                "reaction_policy": bool((memo.get(tid) or {}).get("reaction_policy")),
            })
    shopping.sort(key=lambda r: r["line_cost"] or 0.0, reverse=True)

    # Blueprints you don't own are a real cost of building, so they're part of the total — and they
    # already counted against the margin-saver above, so a component whose print costs more than
    # building saves was bought instead of appearing here.
    blueprint_cost = sum(info.get("blueprint_cost", 0.0) for info in agg.values())

    # Copies bought to fill a SLOT rather than to cover a RUN — reported on their own line, never
    # folded into the figure above. One print is one job at a time, so a batch that wants six
    # concurrent jobs off one copy has to buy five more prints; that is real money, spent on speed
    # rather than on the ability to build at all, and a purchase the builder did not ask for has to
    # be visible and attributable. Same rule as `marginal_saving` and the `blacklisted` badge: report
    # what the convenience cost.
    from app.industry.bpc import cost_for_copies
    blueprint_parallel = []
    parallel_cost = 0.0
    for tid, n in parallel.items():
        acq = (params.bp_acquire or {}).get(tid) or {}
        if n <= 0 or acq.get("kind") != "bpc":
            continue
        # Skip the contracts the run-shortfall purchase already spent — one listing is one item.
        spent = ((agg.get(tid) or {}).get("blueprint_buy") or {}).get("copies") or 0
        c = cost_for_copies(acq, n, skip=spent)
        parallel_cost += c["cost"]
        blueprint_parallel.append({"type_id": tid, "name": names.get(tid, str(tid)),
                                   "copies": n, "cost": c["cost"], "covered": c["covered"],
                                   "jobs": len(by_type.get(tid) or ())})
    blueprint_parallel.sort(key=lambda r: -r["cost"])

    # The other half of the same constraint: prints the plan is SHORT of and will not buy. A
    # reaction formula is durable — it is reused by every build after this one — so charging one to
    # this build would be the same nonsense `acquisition_costs` already refuses for an original.
    # Report what another one would be worth in time and let the builder decide.
    print_limits = sorted(
        ({"type_id": tid, "name": names.get(tid, str(tid)),
          "noun": "formula" if g["activity"] == "reaction" else "blueprint", **g}
         for tid, g in gaps.items()),
        key=lambda r: -(r["hours"] - r["hours_if_held"]))

    # Leftover intermediates (batch-rounding overproduction) are reusable/sellable inventory, not a
    # cost of the finished product — value them at their build unit cost and credit that back so the
    # net product cost excludes what we can recycle.
    leftovers = []
    leftover_value = 0.0
    for tid, info in agg.items():
        if info.get("leftover", 0) <= 0:
            continue
        uc = (memo.get(tid) or {}).get("build_unit_cost") or 0.0
        val = info["leftover"] * uc
        leftover_value += val
        leftovers.append({"type_id": tid, "name": names.get(tid, str(tid)),
                          "qty": info["leftover"], "value": round(val, 2)})
    leftovers.sort(key=lambda r: r["value"], reverse=True)
    total_cost = materials_cost + job_cost + blueprint_cost + parallel_cost
    return {
        # `unit_cost` is the standalone resolved cost of one unit (already memoised above). The
        # aggregate cost below is a shared-batch figure with no per-order split, so this is what
        # lets a caller apportion it — see the per-order margin blend in orders.py.
        "targets": [{"type_id": t, "name": names.get(t, str(t)), "quantity": q,
                     "rank": i, "finish_hours": _finish_of(tasks, t),
                     "unit_cost": round((unit(t, frozenset()) or {}).get("unit_cost") or 0.0, 2)}
                    for i, (t, q) in enumerate(targets)],
        # Per-type build requirements — what progress tracking compares real ESI jobs against.
        # Exposed from `agg` because it's the only place the shared-batch run count exists.
        "requirements": [
            {"type_id": tid, "name": names.get(tid, str(tid)), "activity": info["activity"],
             "runs": info["runs"], "output_qty": info["output_qty"],
             "units": info["runs"] * info["output_qty"],
             # What this step was costed and timed at, and where that came from — so an assumed
             # ME/TE is visible and correctable rather than an invisible input to every number.
             # The batch's runs-weighted ME/TE across the copies it consumes. Per-JOB values are
             # on the scheduled jobs; a batch spanning copies of mixed research has no single one.
             "me": round(params.me_te_for(tid, info["activity"], info["runs"])[0], 3),
             "te": round(params.me_te_for(tid, info["activity"], info["runs"])[1], 3),
             "me_source": (params.me_source.get(tid, "default")
                           if info["activity"] == "manufacturing" else "reaction"),
             # You cannot install a job without the blueprint, so a build step you own nothing for
             # isn't a plan — it's a shopping trip you haven't been told about. Reactions use a
             # formula, not a blueprint, so they're never flagged.
             "blueprint": blueprint_summary(params.owned.get(tid)),
             "needs_blueprint": info["activity"] == "manufacturing" and tid not in params.owned,
             # Owned, but not for this many runs. A 4-run copy against a 20-run batch is sixteen
             # runs still to find, which "you own the blueprint" hides completely.
             "runs_short": info.get("runs_short") or 0,
             # Runs needed (`runs`), runs the copy you hold carries (`blueprint.runs`) and copies
             # you must buy are THREE different numbers. The UI was only ever given the first two,
             # so it had to render the batch's run count beside the word BPC — which a capital
             # builder reads as "the plan found me a 2-run Phoenix copy". Capital copies are 1 run;
             # what the plan meant was "this order is 2 hulls". Say all three or say none.
             "copies_to_buy": (info.get("blueprint_buy") or {}).get("copies") or 0,
             # ...and a FOURTH, which is not any of those three: copies bought so this batch can run
             # in several slots at once. A print is locked while a job runs on it, so parallelism has
             # to be bought in prints — attributed per type here, priced in `blueprint_parallel`.
             "copies_for_slots": parallel.get(tid, 0),
             # The structure this step's ME/TE, fee and duration were all resolved against.
             "site": (params.site_for(tid, info["activity"]) or {}).get("name")}
            for tid, info in agg.items() if info["build"] and info["runs"] > 0
        ],
        "schedule": sched,
        # Where the plan spread itself. Empty on an unrouted plan (one facility), so nothing new
        # appears for an account with one structure. The per-move haul list this used to carry
        # beside it was dropped: the builder knows parts routed to two structures have to travel.
        "build_sites": _sites_used(agg, params),
        # Pins the account set that this build could not honour — the structure is gone, it doesn't
        # run that activity, or routing is off. Reported rather than silently ignored: a pin the
        # plan dropped without saying so is worse than no pin at all.
        "build_pins_unapplied": list(getattr(params, "pin_notes", []) or []),
        "shopping_list": shopping,
        # What the account's reaction policy cost this build — or, when an order overrides it, what
        # reacting saved. Reported rather than quietly taken, same rule as `marginal_saving`.
        "reaction_policy": reaction_policy_report(
            memo, params, names,
            [(tid, info["gross"], bool(info["build"])) for tid, info in agg.items()]),
        # Blueprint copies bought for PARALLELISM, itemised. Kept out of `shopping_list` (which is
        # materials, priced off the market) and out of `missing_blueprints` (which is "you can't
        # build this without one") because it is neither: it is what running the batch side by side
        # cost, and the builder has to be able to see it and argue with it.
        "blueprint_parallel": blueprint_parallel,
        # Prints the plan is short of and deliberately does NOT buy — a reaction formula above all.
        # Advice with a number on it, not a line item: nothing here is in any cost.
        "print_limits": print_limits,
        # Reaction steps whose job-length ceiling could not be met, because meeting it needs more
        # concurrent jobs than the reactor pool or the formulas held can supply. Empty when no
        # ceiling is set and empty when every reaction kept to it.
        "job_length_limits": _job_length_limits(tasks, names),
        # ...and the sibling state: whether the schedule was allowed to count prints at all. On an
        # account whose blueprint picture is incomplete it assumes UNLIMITED prints, exactly as it
        # did before any of this — and says so, with the number of characters still to connect.
        # Not saying it would be its own kind of lie once the user knows the cap exists.
        "print_coverage": {**(params.blueprint_coverage
                              or {"characters": 0, "cached": 0, "missing": 0, "complete": True}),
                           "prints_counted": params.prints_known()},
        "leftovers": leftovers,
        "unresolved": [s["type_id"] for s in shopping if s["unit_price"] is None],
        "metrics": {
            "materials_cost": round(materials_cost, 2),
            "job_cost": round(job_cost, 2),
            "blueprint_cost": round(blueprint_cost, 2),
            # Separate from the line above on purpose — see `blueprint_parallel`.
            "blueprint_parallel_cost": round(parallel_cost, 2),
            "blueprint_parallel_copies": sum(r["copies"] for r in blueprint_parallel),
            # How many steps are running in fewer jobs than the slots would allow because there
            # aren't the prints to install them on. Nothing is bought for these — see `print_limits`.
            "print_limited_steps": len(print_limits),
            # ...and how many reaction steps could not be held to the account's job-length ceiling
            # because the slots or the formulas to run them side by side are not there.
            "job_length_limited_steps": len(_job_length_limits(tasks, names)),
            "total_cost": round(total_cost, 2),
            "leftover_value": round(leftover_value, 2),
            "net_cost": round(total_cost - leftover_value, 2),
            "job_count": len(tasks),
            "build_steps": len(by_type),      # distinct things to build (parallel splits collapsed)
            "makespan_hours": sched["makespan_hours"],
            # When the FIRST queued order is done — the number that matters when you owe someone a
            # delivery, as distinct from when the whole queue drains.
            "first_delivery_hours": (_finish_of(tasks, targets[0][0]) if targets else 0.0),
            # Blueprints you don't own that the plan still builds — priced into total_cost above
            # when copies are listed. Anything with no copies available was flipped to buy instead.
            "missing_blueprints": sorted(
                ({"type_id": tid, "name": names.get(tid, str(tid)),
                  "runs_needed": info["runs"],
                  # None when nothing is listed to buy — the UI says "no copies listed" rather
                  # than implying a number it doesn't have.
                  "copies": (info.get("blueprint_buy") or {}).get("copies"),
                  "cost": (info.get("blueprint_buy") or {}).get("cost"),
                  "covered": (info.get("blueprint_buy") or {}).get("covered")}
                 for tid, info in agg.items()
                 if info["build"] and info["runs"] > 0
                 and info["activity"] == "manufacturing" and tid not in params.owned),
                key=lambda x: x["name"]),
            "slots": pools,
            # What the marginal rule actually resolved to for THIS build, so the UI can show the
            # consequence of the setting in ISK rather than a bare percentage.
            "marginal_threshold": round(marginal_threshold(memo, targets, params), 2),
            "marginal_pct": params.marginal_pct_of_total,
            # What to quote. Net cost is the base: the leftovers this build over-produces stay with
            # the builder and are already credited out of it, so charging them to the customer would
            # bill the same materials twice.
            "margin_pct": params.margin_pct,
            "price": round((total_cost - leftover_value) * (1 + params.margin_pct / 100.0), 2),
        },
    }



def skill_tier(eligibility: dict | None):
    """(character_id, type_id) -> 2 proven capable / 1 unknown / 0 proven incapable.

    Shared so the schedule-wide assignment and the start-now checklist rank candidates the SAME
    way. They legitimately differ on capacity — the schedule spans days and counts total slots, the
    checklist is about what fits right now and counts free ones — but there is no version of this
    where one of them is allowed to name somebody who cannot install the job while the other marks
    it blocked. That disagreement is what made the main screen tell you to start a job the plan
    beside it had already flagged.
    """
    capable = (eligibility or {}).get("capable") or {}
    unknown = (eligibility or {}).get("unknown") or set()

    def tier(cid, type_id) -> int:
        # A step absent from `capable` was never analysed (no recipe match), so nobody is penalised.
        if type_id not in capable:
            return 1
        if cid in capable[type_id]:
            return 2
        return 1 if cid in unknown else 0
    return tier


def assign_characters(waves: list[dict], characters: list[dict],
                      eligibility: dict | None = None) -> list[dict]:
    """Stamp `character_id` / `character_name` onto every scheduled job, across the WHOLE schedule.

    The to-install checklist already named a character for the jobs you can start right now, but
    everything after that was anonymous: a plan would say "stage 1: 12 jobs" and never say who
    installs them, which is not an instruction anyone can follow.

    The scheduler places jobs into two anonymous slot pools whose sizes are the sum of the
    characters' own slots, so an aggregate-feasible schedule is always assignable per character —
    slots are interchangeable. This walks the waves in time order, releasing a character's slot when
    that job ends, and gives each job to whoever has the most capacity free at that moment (which
    spreads the work rather than hammering one toon).

    `eligibility` (optional, from app.industry.skills.analyze_plan_skills) makes this SKILL-AWARE.
    Without it the assignment is capacity-only, which could hand a Revelation to a character with no
    capital production skills — a schedule nobody can execute. With it, candidates are tiered:

      1. proven capable of that step   2. skills unknown (never scanned)   3. proven incapable

    Capacity still decides WITHIN a tier, so the work spreads exactly as before among equals. A
    lower tier is used only when no better candidate has a free slot: a job assigned to someone who
    cannot install it is still more useful than an unassigned one, because the plan stays complete
    and the job carries `skill_ok: False` saying precisely what is wrong. Tasks are stamped with
    `skill_ok` True/False/None (None = unknown), and it is left absent entirely when no eligibility
    was supplied, so "not checked" never renders as "fine".

    Pure and I/O-free like everything else here: the caller supplies the characters. Jobs stay
    unassigned when there's no capacity or no character data, rather than inventing an assignee.
    """
    _tier = skill_tier(eligibility)
    cap: dict[tuple[int, str], int] = {}
    names: dict[int, str] = {}
    for c in characters or []:
        cid = c.get("character_id")
        if cid is None:
            continue
        names[cid] = c.get("character_name") or str(cid)
        for act, key in (("manufacturing", "manufacturing_slots"), ("reaction", "reaction_slots")):
            cap[(cid, act)] = max(0, int(c.get(key) or 0))
    if not cap:
        return waves

    free = dict(cap)
    releases: list[tuple[float, int, str]] = []      # (end_hours, character_id, activity)
    for w in waves:
        start = w.get("start_hours") or 0.0
        # Hand back every slot whose job has finished by the time this wave starts.
        still = []
        for end, cid, act in releases:
            if end <= start + 1e-9:
                free[(cid, act)] = free.get((cid, act), 0) + 1
            else:
                still.append((end, cid, act))
        releases = still
        for t in w.get("tasks", []):
            act = t.get("activity")
            tid = t.get("type_id")
            best, best_key = None, None
            for (cid, a), n in free.items():
                if a != act or n <= 0:
                    continue
                # Skill tier first, then most free capacity, then character_id so the result stays
                # deterministic. Negated because bigger is better and we're taking the minimum.
                key = (-_tier(cid, tid), -n, cid)
                if best_key is None or key < best_key:
                    best, best_key = cid, key
            if best is None:
                t["character_id"] = None
                t["character_name"] = None
                if eligibility is not None:
                    t["skill_ok"] = None
                continue
            if eligibility is not None:
                tier = _tier(best, tid)
                t["skill_ok"] = True if tier == 2 else (None if tier == 1 else False)
            free[(best, act)] -= 1
            releases.append((start + (t.get("duration_hours") or 0.0), best, act))
            t["character_id"] = best
            t["character_name"] = names.get(best)
    return waves


# Slider stops the UI offers for the marginal-saving knob (0–10% in 0.5 steps).
MARGINAL_SWEEP_PCTS = [round(i * 0.5, 1) for i in range(21)]


def sweep_marginal(targets: list[tuple[int, int]], mfg: dict, rx: dict, prices: dict,
                   adjusted: dict, params: BuildParams, names: dict[int, str], pools: dict[str, int],
                   on_hand: dict[int, float] | None = None,
                   pcts: list[float] | None = None) -> list[dict]:
    """Cost + makespan at every stop of the marginal-saving slider, so the UI can show what dragging
    it actually buys you (time saved, ISK spent) without a round trip per pixel.

    The knob only ever moves the ISK threshold `marginal_threshold()` resolves to, and that value
    is floored at `min_saving_isk` — so on a small build every low percentage collapses to the same
    threshold and therefore the same plan. Deduping by the resolved threshold means the sweep runs
    a handful of plans, not one per stop."""
    pcts = pcts if pcts is not None else MARGINAL_SWEEP_PCTS
    # marginal_threshold needs a priced memo, and resolve_unit_costs is marginal-independent (the
    # knob is applied later, in aggregate_demand) — so one memo serves every stop.
    memo, unit = resolve_unit_costs(mfg, rx, prices, adjusted, params)
    for tid, _ in targets:
        unit(tid, frozenset())
    by_threshold: dict[float, dict] = {}
    out = []
    for pct in pcts:
        p = copy.copy(params)
        p.marginal_pct_of_total = pct
        thr = round(marginal_threshold(memo, targets, p), 2)
        point = by_threshold.get(thr)
        if point is None:
            m = plan_queue(targets, mfg, rx, prices, adjusted, p, names, pools,
                           on_hand=dict(on_hand or {}))["metrics"]
            point = {
                "threshold": thr,
                "total_cost": m["total_cost"],
                "net_cost": m["net_cost"],
                "makespan_hours": m["makespan_hours"],
                "build_steps": m["build_steps"],
                "job_count": m["job_count"],
            }
            by_threshold[thr] = point
        out.append({"pct": pct, **point})
    return out


# ── Per-order planning (behind `industry_per_order_plans`) ────────────────────────────────────

def _order_params(params: BuildParams, spec: dict) -> BuildParams:
    """This order's own build parameters: its forced builds, its reaction override, its ME/TE.

    In the aggregated plan these have to be unioned across the queue — one shared batch per
    component can only be built one way — which is exactly the compromise per-order planning
    removes. Here each order carries its own.
    """
    p = copy.copy(params)
    p.force_build_ids = set(params.force_build_ids) | {int(t) for t in (spec.get("force_build_ids") or [])}
    p.build_reactions_anyway = bool(params.build_reactions_anyway or spec.get("build_reactions"))
    p.me_by_product = dict(params.me_by_product)
    p.me_source = dict(params.me_source)
    for k, v in (spec.get("me_te_overrides") or {}).items():
        try:
            p.me_by_product[int(k)] = (float(v[0]), float(v[1]))
            p.me_source[int(k)] = "override"
        except Exception:
            continue
    return p


def plan_queue_per_order(order_specs: list[dict], mfg: dict, rx: dict, prices: dict, adjusted: dict,
                         params: BuildParams, names: dict, pools: dict[str, int],
                         on_hand: dict[int, float] | None = None) -> dict:
    """Plan every order on its OWN, then schedule the lot together against the shared slot pool.

    Why this exists, in one physical fact: **a job outputs to exactly one container.** Builders run
    a container per build — it is where the materials are sourced and where the output lands, and it
    is how they know what is finished for which customer with three orders in flight. A batch shared
    between two orders has nowhere to deliver. The aggregated plan (`plan_queue`) is cheaper and
    cannot be executed that way.

    What it costs: shared components are built once PER ORDER, blueprint copies are bought per
    order, and output rounding (a reaction making 2/run) is wasted per order rather than once. That
    is the price of attribution and it is real — hence `/api/industry/queue-plan/compare`, which
    quotes both plans' cost, makespan and job count off the same inputs.

    `order_specs`: [{id, type_id, quantity, force_build_ids, me_te_overrides, build_reactions,
    margin_pct, stock}] in queue order.

    **Stock is allocated first come first served down that list**, because two orders cannot both
    spend the same hangar contents and queue order is the only fair way to decide who gets it. An
    order that curated its own containers (`stock`) is additionally capped by them — its own boxes
    say what it MAY spend, the queue-wide remainder says what is still THERE, and it spends the
    lower of the two. Only what a batch is actually netted against is deducted: `aggregate_demand`
    nets stock off BUILT types (fewer runs), so that is what leaves the pool.

    **Returns the same shape as `plan_queue`**, deliberately — the checklist, the progress overlay,
    the customer share and the whole build page read that contract, and a per-order plan that spoke
    a different one would silently strip half the page. Per-type reporting rows are merged across
    orders (runs summed); `per_order` carries the per-order costs the aggregated plan cannot give,
    and `metrics.price` is their real sum rather than a pro-rata apportionment (`_blend_margin`
    exists only because a shared batch has no per-order cost, so it does not run on this path).
    """
    from app.industry.bpc import cost_for_copies

    remaining_stock = dict(on_hand or {})
    ctxs: list[dict] = []
    # Contracts already spent by an earlier order in this queue. A blueprint copy on contract is ONE
    # item: two orders cannot both buy it, and each order pricing the market from scratch is exactly
    # how they would. Measured on a real 2x Phoenix queue split in two, that made the split look
    # 76.8% CHEAPER on blueprints than building it as one batch — both orders taking the same cheap
    # listing. Same rule as the stock allocation above, and for the same physical reason.
    spent_listings: dict[int, list[dict]] = {}

    # ...and the same rule for the copies the builder already OWNS. A blueprint copy carries a fixed
    # number of runs and they are spent when they are run: crediting the same 2-run copy to two
    # orders reports a shortfall of zero twice and buys nothing, which on a real 2x Phoenix queue
    # split in two made the split look 76.7% cheaper on blueprints than the single batch. Originals
    # are NOT consumed (they run forever) — only copies are.
    owned_used: dict[int, int] = {}

    def _pool_for(p: BuildParams) -> BuildParams:
        """`p` with every contract bought and every copy-run spent by an earlier order removed."""
        if not spent_listings and not owned_used:
            return p
        p = copy.copy(p)
        if spent_listings and p.bp_acquire:
            acq = dict(p.bp_acquire)
            for tid_, used in spent_listings.items():
                info = acq.get(tid_)
                if not info or not used:
                    continue
                gone = {id(l) for l in used}
                acq[tid_] = {**info, "listings": [l for l in (info.get("listings") or [])
                                                  if id(l) not in gone]}
            p.bp_acquire = acq
        if owned_used and p.owned:
            owned = dict(p.owned)
            for tid_, spent in owned_used.items():
                entry = owned.get(tid_)
                if not entry or spent <= 0 or entry.get("kind") == "bpo":
                    continue
                left, rest = spent, []
                # `copies` is already in consumption order (best researched first), which is the
                # order the jobs took them in, so spending off the front is what actually happened.
                for cp in (entry.get("copies") or []):
                    runs_c = int(cp.get("runs") or 0)
                    if runs_c < 0:
                        rest.append(cp)          # an original among copies — never used up
                        continue
                    if left >= runs_c:
                        left -= runs_c
                        continue
                    rest.append({**cp, "runs": runs_c - left} if left else cp)
                    left = 0
                if not rest:
                    owned.pop(tid_, None)        # nothing left — the next order has to buy
                else:
                    owned[tid_] = {**entry, "copies": rest, "copy_count": len(rest),
                                   "me": rest[0]["me"], "te": rest[0]["te"],
                                   "runs": sum(max(0, int(c.get("runs") or 0)) for c in rest)}
            p.owned = owned
        return p

    # ── Pass 1: each order's own demand, and the packing it would do alone ───────────────────
    for idx, spec in enumerate(order_specs):
        tid, qty = int(spec["type_id"]), int(spec["quantity"])
        p = _pool_for(_order_params(params, spec))
        memo, unit = resolve_unit_costs(mfg, rx, prices, adjusted, p)
        unit(tid, frozenset())

        own = spec.get("stock")
        pool = ({t: min(v, remaining_stock.get(t, 0.0)) for t, v in own.items()}
                if own is not None else dict(remaining_stock))
        agg = aggregate_demand([(tid, qty)], memo, mfg, rx, p, pool, pools)
        for t, info in agg.items():
            if info["build"]:
                spent = min(pool.get(t, 0.0), info["gross"])
                if spent > 0:
                    remaining_stock[t] = max(0.0, remaining_stock.get(t, 0.0) - spent)

        # Whatever this order's run-shortfall purchase took is gone for everyone behind it, and so
        # are the runs it spent off the copies the account owns.
        for t, info in agg.items():
            for l in ((info.get("blueprint_buy") or {}).get("used") or ()):
                spent_listings.setdefault(t, []).append(l)
            own_t = (p.owned or {}).get(t) or {}
            if info.get("build") and own_t and own_t.get("kind") != "bpo":
                used = max(0, int(info.get("runs") or 0) - int(info.get("runs_short") or 0))
                if used:
                    owned_used[t] = owned_used.get(t, 0) + used

        o_deps = _built_deps(agg, mfg, rx)
        depths = _depths([tid], mfg, rx)
        o_plan: dict[int, dict] = {}
        o_start: dict[int, float] = {}
        build_tasks(agg, mfg, rx, p, pools, depths=depths, deps=o_deps, align=False,
                    plan_out=o_plan, start_out=o_start)
        ctxs.append({"idx": idx, "spec": spec, "type_id": tid, "quantity": qty, "params": p,
                     "memo": memo, "unit": unit, "agg": agg, "deps": o_deps, "depths": depths,
                     "plan": o_plan, "start": o_start})

    # ── Cross-order alignment, which now has to be explicit ─────────────────────────────────
    # In the aggregated plan a builder logs in once because every order's jobs were packed in one
    # call and `_align_cohorts` saw them together. Planned separately they never meet, so the same
    # queue would land its jobs at as many different moments as it has orders — the effort cost this
    # whole feature is supposed to fit inside. Align the union, keyed per order so one order's
    # cohort can never be confused with another's, then replay each order with the answer.
    joint_plan = {(c["idx"], t): pl for c in ctxs for t, pl in c["plan"].items()}
    joint_start = {(c["idx"], t): c["start"].get(t, 0.0) for c in ctxs for t in c["plan"]}
    _align_cohorts(joint_plan, joint_start)
    hints = [{t: pl["aligned_jobs"] for t, pl in c["plan"].items() if pl.get("aligned_jobs")}
             for c in ctxs]

    def _emit(use_hints: bool) -> dict:
        """Build and schedule every order's jobs, with or without the cross-order alignment."""
        all_tasks: list[Task] = []
        by_key: dict[object, list[Task]] = {}
        deps: dict[object, set] = {}
        priority: dict[object, tuple] = {}
        per_ctx: list[dict] = []
        for c in ctxs:
            idx = c["idx"]
            o_parallel: dict[int, int] = {}
            o_gaps: dict[int, dict] = {}
            tasks, o_by_type = build_tasks(
                c["agg"], mfg, rx, c["params"], pools, depths=c["depths"], deps=c["deps"],
                align=False, parallel_copies=o_parallel, print_gaps=o_gaps,
                align_hint=(hints[idx] if use_hints else None))
            crit = _critical_priority(c["agg"], c["deps"], mfg, rx, c["params"])
            for t in tasks:
                t.key = (idx, t.type_id)
                t.order_id = c["spec"].get("id")
                t.task_id = f"{idx}:{t.task_id}"
                by_key.setdefault(t.key, []).append(t)
                # Earlier orders outrank later ones — FIFO across the queue, criticality within it.
                priority[t.key] = (-idx, crit.get(t.type_id, 0.0))
            for dtid, ds in c["deps"].items():
                deps[(idx, dtid)] = {(idx, d) for d in ds}
            all_tasks.extend(tasks)
            per_ctx.append({"tasks": tasks, "by_type": o_by_type,
                            "parallel": o_parallel, "gaps": o_gaps})
        sched = schedule(all_tasks, by_key, deps, pools, priority)
        return {"tasks": all_tasks, "by_key": by_key, "sched": sched, "per_ctx": per_ctx}

    built = _emit(True)
    # Same give-back rule as `plan_queue`, and for the same reason: alignment is measured on the
    # SCHEDULED makespan, the number actually quoted, because that is the only place slot
    # contention is real. If the merge cost the delivery more than it is allowed to, drop the lot.
    if any(t.why and t.why.get("bound_by") == "aligned" for t in built["tasks"]):
        plain = _emit(False)
        if built["sched"]["makespan_hours"] > plain["sched"]["makespan_hours"] * (1 + _DELIVERY_OVERSHOOT):
            built = plain

    sched = built["sched"]
    by_order = {c["spec"].get("id"): c for c in ctxs}
    for w in sched["waves"]:
        for t in w["tasks"]:
            t["name"] = names.get(t["type_id"], str(t["type_id"]))
            # Which order this job belongs to — the whole point of planning them apart. Without it
            # the checklist is back to "install 40 runs of Capital Armor Plates" with three builds
            # in flight and no way to say which container they belong in.
            c = by_order.get(t.get("order_id"))
            if c is not None:
                t["order_type_id"] = c["type_id"]
                t["order_name"] = names.get(c["type_id"], str(c["type_id"]))
            site = (c["params"] if c else params).site_for(t["type_id"], t["activity"])
            if site:
                t["site"] = site["name"]
                t["site_key"] = site["key"]
                # A pinned job says so. "I chose this building" and "the tool worked it out" are
                # different facts about the same line, and only one of them is worth arguing with.
                if site.get("pinned"):
                    t["site_pinned"] = site["pinned"]
            if t.get("why") and t["why"].get("needed_by") is not None:
                t["why"]["needed_by_name"] = names.get(t["why"]["needed_by"],
                                                       str(t["why"]["needed_by"]))

    # ── Roll-up ─────────────────────────────────────────────────────────────────────────────
    totals = {"materials_cost": 0.0, "job_cost": 0.0, "blueprint_cost": 0.0,
              "blueprint_parallel_cost": 0.0, "total_cost": 0.0, "leftover_value": 0.0,
              "net_cost": 0.0}
    per_order: list[dict] = []
    built_rows: dict[int, dict] = {}
    shopping: dict[int, dict] = {}
    leftovers: dict[int, dict] = {}
    parallel_rows: dict[int, dict] = {}
    gaps: dict[int, dict] = {}
    missing: dict[int, dict] = {}
    sites: dict[str, dict] = {}
    price = 0.0
    par_spent: dict[int, int] = {}
    rates: set[float] = set()
    reaction_reports: list[dict] = []

    for c in ctxs:
        idx, p, agg, memo = c["idx"], c["params"], c["agg"], c["memo"]
        pc = built["per_ctx"][idx]

        # Blueprint copies bought so THIS order's batches run side by side. Per-order plans buy
        # prints per order by design — that is the price of attribution — and it stays its own line.
        o_par_cost = 0.0
        for ptid, n in pc["parallel"].items():
            acq = (p.bp_acquire or {}).get(ptid) or {}
            if n <= 0 or acq.get("kind") != "bpc":
                continue
            # Skip what this order's own run-shortfall purchase spent, AND what every earlier
            # order's parallelism purchase did — one contract, one buyer, whichever list it is on.
            spent = (((agg.get(ptid) or {}).get("blueprint_buy") or {}).get("copies") or 0)
            cc = cost_for_copies(acq, n, skip=spent + par_spent.get(ptid, 0))
            par_spent[ptid] = par_spent.get(ptid, 0) + n
            o_par_cost += cc["cost"]
            row = parallel_rows.setdefault(ptid, {"type_id": ptid, "name": names.get(ptid, str(ptid)),
                                                  "copies": 0, "cost": 0.0, "covered": 0, "jobs": 0})
            row["copies"] += n
            row["cost"] += cc["cost"]
            row["covered"] += cc["covered"]
            row["jobs"] += len(pc["by_type"].get(ptid) or ())

        cost = _order_cost(agg, mfg, rx, prices, adjusted, p, memo)
        cost["blueprint_parallel_cost"] = round(o_par_cost, 2)
        cost["total_cost"] += o_par_cost
        cost["net_cost"] += o_par_cost
        for k in totals:
            totals[k] += cost.get(k, 0.0)
        margin = c["spec"].get("margin_pct")
        margin = params.margin_pct if margin is None else float(margin)
        rates.add(round(margin, 4))
        # Per-order planning gives every order its own REAL cost, so its price is arithmetic on
        # that — no apportionment, no blended rate standing in for one.
        o_price = cost["net_cost"] * (1 + margin / 100.0)
        price += o_price
        per_order.append({"order_id": c["spec"].get("id"), "type_id": c["type_id"],
                          "name": names.get(c["type_id"], str(c["type_id"])),
                          "quantity": c["quantity"], "jobs": len(pc["tasks"]),
                          "margin_pct": round(margin, 4), "price": round(o_price, 2),
                          "finish_hours": _finish_of(pc["tasks"], c["type_id"]),
                          **{k: round(v, 2) for k, v in cost.items()}})

        for tid, info in agg.items():
            if info["build"] and info["runs"] > 0:
                row = built_rows.get(tid)
                if row is None:
                    row = built_rows[tid] = {
                        "type_id": tid, "name": names.get(tid, str(tid)),
                        "activity": info["activity"], "runs": 0, "output_qty": info["output_qty"],
                        "units": 0, "me": 0.0, "te": 0.0,
                        "me_source": (p.me_source.get(tid, "default")
                                      if info["activity"] == "manufacturing" else "reaction"),
                        "blueprint": blueprint_summary(p.owned.get(tid)),
                        "needs_blueprint": info["activity"] == "manufacturing" and tid not in p.owned,
                        "runs_short": 0, "copies_to_buy": 0, "copies_for_slots": 0,
                        "site": (p.site_for(tid, info["activity"]) or {}).get("name"),
                        # Which orders this step is for. The aggregated plan cannot say — that is
                        # the whole point — and the container a job delivers to hangs off it.
                        "orders": [],
                    }
                row["runs"] += info["runs"]
                row["units"] += info["runs"] * info["output_qty"]
                row["runs_short"] += info.get("runs_short") or 0
                row["copies_to_buy"] += (info.get("blueprint_buy") or {}).get("copies") or 0
                row["copies_for_slots"] += pc["parallel"].get(tid, 0)
                row["orders"].append(c["spec"].get("id"))
                me, te = p.me_te_for(tid, info["activity"], info["runs"])
                row["me"], row["te"] = round(me, 3), round(te, 3)
                site = p.site_for(tid, info["activity"])
                if site:
                    srow = sites.setdefault(site["key"], {"key": site["key"], "name": site["name"],
                                                          "system_id": site.get("system_id"),
                                                          "steps": 0, "pinned": False})
                    srow["steps"] += 1
                    srow["pinned"] = srow["pinned"] or bool(site.get("pinned"))
                if (info["activity"] == "manufacturing" and tid not in p.owned):
                    mrow = missing.setdefault(tid, {"type_id": tid, "name": names.get(tid, str(tid)),
                                                    "runs_needed": 0, "copies": None,
                                                    "cost": None, "covered": None})
                    mrow["runs_needed"] += info["runs"]
                    buy = info.get("blueprint_buy") or {}
                    if buy.get("copies") is not None:
                        mrow["copies"] = (mrow["copies"] or 0) + buy["copies"]
                        mrow["cost"] = (mrow["cost"] or 0.0) + (buy.get("cost") or 0.0)
                        mrow["covered"] = (mrow["covered"] or 0) + (buy.get("covered") or 0)
            elif not info["build"] and info["gross"] > 0:
                srow = shopping.get(tid)
                if srow is None:
                    price_each = (prices.get(tid) or {}).get("sell_price")
                    srow = shopping[tid] = {
                        "type_id": tid, "name": names.get(tid, str(tid)), "qty": 0.0,
                        "unit_price": price_each, "source": (prices.get(tid) or {}).get("source"),
                        "line_cost": None, "bought_for_speed": False, "bought_marginal": False,
                        "marginal_saving": None, "bought_no_blueprint": False,
                        "blacklisted": tid in p.never_build_ids, "reaction_policy": False,
                    }
                srow["qty"] += info["gross"]
                srow["bought_for_speed"] |= bool(info.get("bought_for_speed"))
                srow["bought_marginal"] |= bool(info.get("bought_marginal"))
                srow["bought_no_blueprint"] |= bool(info.get("bought_no_blueprint"))
                srow["reaction_policy"] |= bool((memo.get(tid) or {}).get("reaction_policy"))
                if info.get("marginal_saving") is not None:
                    srow["marginal_saving"] = round((srow["marginal_saving"] or 0.0)
                                                    + info["marginal_saving"], 2)
            if info.get("leftover", 0) > 0:
                uc = (memo.get(tid) or {}).get("build_unit_cost") or 0.0
                lrow = leftovers.setdefault(tid, {"type_id": tid, "name": names.get(tid, str(tid)),
                                                  "qty": 0.0, "value": 0.0})
                lrow["qty"] += info["leftover"]
                lrow["value"] = round(lrow["value"] + uc * info["leftover"], 2)

        for tid, g in pc["gaps"].items():
            # The worst shortfall wins: the same formula limits every order that needs it, and the
            # one that would gain most from another is the one worth reporting.
            prev = gaps.get(tid)
            if prev is None or (g["hours"] - g["hours_if_held"]) > (prev["hours"] - prev["hours_if_held"]):
                gaps[tid] = {"type_id": tid, "name": names.get(tid, str(tid)),
                             "noun": "formula" if g["activity"] == "reaction" else "blueprint", **g}
        rp = reaction_policy_report(
            c["memo"], p, names,
            [(tid, info["gross"], bool(info["build"])) for tid, info in agg.items()])
        if rp:
            reaction_reports.append(rp)

    for row in shopping.values():
        row["line_cost"] = (row["unit_price"] or 0.0) * row["qty"] if row["unit_price"] else None

    # Targets stay keyed by TYPE, as in the aggregated plan: every reader of this contract looks a
    # product up by type_id, and two orders for the same hull still deliver at whichever of them
    # finishes last. The per-order split lives in `per_order`, where it can be read unambiguously.
    targets: list[dict] = []
    seen: dict[int, dict] = {}
    for c in ctxs:
        tid = c["type_id"]
        fin = _finish_of(built["per_ctx"][c["idx"]]["tasks"], tid)
        row = seen.get(tid)
        if row is None:
            row = seen[tid] = {"type_id": tid, "name": names.get(tid, str(tid)),
                               "quantity": 0, "rank": len(targets), "finish_hours": 0.0,
                               "unit_cost": round((c["unit"](tid, frozenset()) or {}).get("unit_cost") or 0.0, 2)}
            targets.append(row)
        row["quantity"] += c["quantity"]
        row["finish_hours"] = max(row["finish_hours"], fin)

    total_cost = totals["total_cost"]
    leftover_value = totals["leftover_value"]
    first = ctxs[0] if ctxs else None
    return {
        "targets": targets,
        "per_order": per_order,
        "requirements": sorted(built_rows.values(), key=lambda r: r["name"]),
        "schedule": sched,
        "build_sites": sorted(sites.values(), key=lambda r: -r["steps"]),
        # Same report on the per-order path: a pin is an account-wide rule, so it has to be answered
        # however the queue was planned.
        "build_pins_unapplied": list(getattr(params, "pin_notes", []) or []),
        "shopping_list": sorted(shopping.values(), key=lambda r: r["line_cost"] or 0.0, reverse=True),
        "reaction_policy": (reaction_reports[0] if len(reaction_reports) == 1 else
                            _merge_reaction_reports(reaction_reports)),
        "blueprint_parallel": sorted(({**r, "cost": round(r["cost"], 2)}
                                      for r in parallel_rows.values()), key=lambda r: -r["cost"]),
        "print_limits": sorted(gaps.values(), key=lambda r: -(r["hours"] - r["hours_if_held"])),
        # Same report on the per-order path — a ceiling is an account-wide rule, so it has to be
        # answered for however the queue was planned.
        "job_length_limits": _job_length_limits(built["tasks"], names),
        "print_coverage": {**(params.blueprint_coverage
                              or {"characters": 0, "cached": 0, "missing": 0, "complete": True}),
                           "prints_counted": params.prints_known()},
        "leftovers": sorted(leftovers.values(), key=lambda r: r["value"], reverse=True),
        "unresolved": [s["type_id"] for s in shopping.values() if s["unit_price"] is None],
        # What planning apart cost, stated rather than discovered: the same queue aggregated is
        # cheaper, and the builder is entitled to see by how much before it is charged to a quote.
        "per_order_plans": True,
        "metrics": {
            "materials_cost": round(totals["materials_cost"], 2),
            "job_cost": round(totals["job_cost"], 2),
            "blueprint_cost": round(totals["blueprint_cost"], 2),
            "blueprint_parallel_cost": round(totals["blueprint_parallel_cost"], 2),
            "blueprint_parallel_copies": sum(r["copies"] for r in parallel_rows.values()),
            "print_limited_steps": len(gaps),
            "job_length_limited_steps": len(_job_length_limits(built["tasks"], names)),
            "total_cost": round(total_cost, 2),
            "leftover_value": round(leftover_value, 2),
            "net_cost": round(total_cost - leftover_value, 2),
            "job_count": len(built["tasks"]),
            "build_steps": len(built["by_key"]),
            "makespan_hours": sched["makespan_hours"],
            "first_delivery_hours": (per_order[0]["finish_hours"] if per_order else 0.0),
            "missing_blueprints": sorted(missing.values(), key=lambda x: x["name"]),
            "slots": pools,
            "marginal_threshold": round(marginal_threshold(
                first["memo"], [(c["type_id"], c["quantity"]) for c in ctxs], params), 2) if first else 0.0,
            "marginal_pct": params.marginal_pct_of_total,
            "margin_pct": (round(rates.pop(), 4) if len(rates) == 1 else
                           (round((price / (total_cost - leftover_value) - 1) * 100.0, 2)
                            if total_cost - leftover_value else 0.0)),
            "margin_mixed": len(rates) > 1,
            "price": round(price, 2),
        },
    }


def _merge_reaction_reports(reports: list[dict]) -> dict | None:
    """One account-level reaction-policy report out of the per-order ones.

    The policy is the ACCOUNT's, so what it cost is the sum of what it cost each order; the same
    reaction bought for two builds is two lines' worth of ISK, not one. `overridden` is true when
    ANY order overrode the policy, matching the aggregated plan's union.
    """
    if not reports:
        return None
    items: dict[int, dict] = {}
    for r in reports:
        for it in (r.get("items") or []):
            row = items.get(it["type_id"])
            if row is None:
                row = items[it["type_id"]] = {**it, "qty": 0.0, "saving": 0.0}
            row["qty"] += it.get("qty") or 0.0
            row["saving"] = round((row["saving"] or 0.0) + (it.get("saving") or 0.0), 2)
            row["built"] = row.get("built") or it.get("built")
    return {
        "overridden": any(r.get("overridden") for r in reports),
        "all": bool(reports[0].get("all")),
        "categories": reports[0].get("categories") or [],
        "items": sorted(items.values(), key=lambda r: -(r.get("saving") or 0.0)),
        "isk": round(sum(r.get("isk") or 0.0 for r in reports), 2),
    }


def _order_cost(agg, mfg, rx, prices, adjusted, params, memo) -> dict:
    """Materials + job fees + blueprint copies for ONE order's own demand, less reusable leftovers.
    Same arithmetic as plan_queue's roll-up; kept here so a per-order plan reports a real cost of
    its own rather than a share of somebody else's."""
    materials = job = 0.0
    for tid, info in agg.items():
        if info["build"]:
            recipe = mfg.get(tid) or rx.get(tid)
            eiv = sum(inp["quantity"] * info["runs"] * adjusted.get(inp["type_id"], 0.0)
                      for inp in recipe["inputs"])
            # THE SAME rate `plan_queue` charges, and it has to be the shared one: this used to
            # reach for `mfg_cost_index`/`rx_cost_index` directly, which ignores per-job ROUTING —
            # a routed job pays its own system's index and its own structure's tax. Measured on a
            # real queued build (context 9022), that one line made planning apart look 6.84% more
            # expensive than aggregating when the two plans were in fact identical: 220.5M of job
            # fees against 511.4M, all of it this function disagreeing with the plan beside it.
            job += eiv * params.job_fee_rate(tid, info["activity"])
        elif info["gross"] > 0:
            materials += ((prices.get(tid) or {}).get("sell_price") or 0.0) * info["gross"]
    blueprint = sum(i.get("blueprint_cost", 0.0) for i in agg.values())
    leftover = 0.0
    for tid, info in agg.items():
        if info.get("leftover", 0) > 0:
            uc = (memo.get(tid) or {}).get("build_unit_cost") or 0.0
            leftover += uc * info["leftover"]
    total = materials + job + blueprint
    return {"materials_cost": materials, "job_cost": job, "blueprint_cost": blueprint,
            "total_cost": total, "leftover_value": leftover, "net_cost": total - leftover}
