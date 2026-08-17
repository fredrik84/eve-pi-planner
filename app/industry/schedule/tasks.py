"""`build_tasks` — the aggregated demand turned into the concrete job list."""
import copy
import math
from collections import defaultdict
from dataclasses import dataclass

from app.industry.graph import (
    BuildParams, blueprint_summary, collect_reachable, effective_material_qty,
    reaction_policy_report, resolve_unit_costs,
)


from app.industry.schedule.splitting import (
    Task,
    _align_cohorts,
    _copy_limits,
    _jobs_on_copies,
    _packed_duration,
    _packed_jobs,
    _print_limits,
    _tightest,
)
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
