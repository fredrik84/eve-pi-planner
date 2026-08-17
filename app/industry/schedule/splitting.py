"""How a type's runs become JOBS: blueprint copy limits, print limits, packing and cohort
alignment. `Task` lives here because it is what this module produces."""
import copy
import math
from collections import defaultdict
from dataclasses import dataclass

from app.industry.graph import (
    BuildParams, blueprint_summary, collect_reachable, effective_material_qty,
    reaction_policy_report, resolve_unit_costs,
)


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
