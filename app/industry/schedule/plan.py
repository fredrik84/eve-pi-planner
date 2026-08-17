"""The orchestrator: demand -> tasks -> schedule -> metrics, plus character assignment and the
marginal sweep. This is what a caller actually asks for."""
import copy
import math
from collections import defaultdict
from dataclasses import dataclass

from app.industry.graph import (
    BuildParams, blueprint_summary, collect_reachable, effective_material_qty,
    reaction_policy_report, resolve_unit_costs,
)


from app.industry.schedule.demand import _depths, aggregate_demand, marginal_threshold
from app.industry.schedule.splitting import _DELIVERY_OVERSHOOT
from app.industry.schedule.tasks import build_tasks
from app.industry.schedule.scheduler import (
    _built_deps,
    _critical_priority,
    _fifo_priority,
    order_ranks,
    schedule,
)
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
