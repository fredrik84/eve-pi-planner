"""Planning each order apart (behind `industry_per_order_plans`), and rolling the results back
up into one queue-wide answer."""
import copy
import math
from collections import defaultdict
from dataclasses import dataclass

from app.industry.graph import (
    BuildParams, blueprint_summary, collect_reachable, effective_material_qty,
    reaction_policy_report, resolve_unit_costs,
)


from app.industry.schedule.demand import _depths, aggregate_demand, marginal_threshold
from app.industry.schedule.splitting import Task, _DELIVERY_OVERSHOOT, _align_cohorts
from app.industry.schedule.tasks import build_tasks
from app.industry.schedule.scheduler import _built_deps, _critical_priority, schedule
from app.industry.schedule.plan import _finish_of, _job_length_limits
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

    # How many physical prints exist per type, handed to `schedule()` so a blueprint is a
    # TIME-SHARED resource: a job needs a free slot and a free print, and the print is released when
    # the job ends. Two orders planned apart contend for the same item because the resource is keyed
    # on the real `type_id`, which is not namespaced per order.
    #
    # An earlier attempt subtracted each order's claim from the next order's cap. That only BOUNDED
    # the over-booking — a claim is permanent while a print is merely busy — so it both over-
    # serialised (a print freed at 1h was unavailable for the rest of the plan) and still allowed N
    # orders one concurrent job each. It was deleted rather than kept alongside this.
    print_caps: dict[int, int] = {}

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
        # Only prints that CANNOT be bought are contended: where more are purchasable the plan buys
        # them, and `spent_listings` above already stops two orders buying the same listing.
        # Recorded once, from the first order that plans the type — nothing has consumed it at that
        # point, so it is the true holding and the result cannot depend on queue order. Only types
        # whose prints CANNOT be bought are contended: where more are purchasable the plan buys them,
        # and `spent_listings` already stops two orders buying the same listing.
        for t_, pl_ in o_plan.items():
            pr_ = pl_.get("prints")
            if pr_ is None or pl_.get("can_buy_prints") or t_ in print_caps:
                continue
            print_caps[t_] = int(pr_)
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
        sched = schedule(all_tasks, by_key, deps, pools, priority, print_caps=print_caps)
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
