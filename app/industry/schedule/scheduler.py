"""The resource-constrained list scheduler: dependencies, critical-path priority, and the
slot-pool simulation that produces the waves."""
import heapq
from collections import defaultdict

from app.industry.graph import BuildParams, collect_reachable
from app.industry.schedule.splitting import Task


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
             priority: dict[int, float], print_caps: dict[int, int] | None = None) -> dict:
    """Event-driven resource-constrained list scheduler. Fills each pool's free slots with the
    highest-priority ready tasks, advancing time to the next job completion when nothing more can
    start. Returns makespan, per-task placements, and waves (tasks grouped by start time).

    `print_caps` makes a BLUEPRINT a second resource alongside the slot pool: `{type_id: how many
    physical prints exist}`. A print is one item and is locked for the duration of the job on it, so
    a job needs a free slot AND a free print to start, and the print comes back when the job ends —
    exactly like a slot, just pooled per type rather than per activity.

    This is what a per-plan cap could not express. Capping concurrency inside one plan cannot see a
    second plan, so two orders planned apart each scheduled jobs off the same original; and
    subtracting an earlier order's claim only bounded it, because a claim is permanent while a print
    is merely busy. Here the print is genuinely handed on when the earlier job finishes, which is
    both correct and cheaper than either approximation.

    Omitted (or a type absent from it) = unlimited, which is the old behaviour and the right default:
    a type we have observed nothing about must never be serialised on absent evidence."""
    free_slots = {activity: list(range(count)) for activity, count in pools.items()}
    caps = dict(print_caps or {})
    prints_free = dict(caps)
    completed: set[int] = set()
    running: list[Task] = []
    started: set[str] = set()
    now = 0.0
    # Priority never changes during the simulation; keep its stable order as tasks leave the queue.
    pending = sorted(tasks, key=lambda t: priority.get(t.sched_key(), (0, 0.0)), reverse=True)

    while pending:
        # Start every ready task that fits a free slot in its pool, most-critical first.
        waiting = []
        for t in pending:
            # A job needs a free SLOT and a free PRINT. `t.type_id` is the product, which is what
            # identifies the blueprint or formula it runs off — and it is shared across orders, so
            # two separately-planned builds contend for the same item here rather than each
            # believing they hold it.
            has_print = t.type_id not in prints_free or prints_free[t.type_id] > 0
            if (free_slots.get(t.activity) and has_print
                    and all(d in completed for d in deps.get(t.sched_key(), ()))):
                t.slot = heapq.heappop(free_slots[t.activity])
                t.start = now
                t.end = now + t.duration
                if t.type_id in prints_free:
                    prints_free[t.type_id] -= 1
                started.add(t.task_id)
                running.append(t)
            else:
                waiting.append(t)
        pending = waiting
        if not running:
            break  # nothing running and nothing startable → unschedulable remainder
        # Advance to the next completion(s), free those slots, mark newly-complete types.
        next_end = min(t.end for t in running)
        now = next_end
        for t in [x for x in running if x.end == next_end]:
            heapq.heappush(free_slots[t.activity], t.slot)
            if t.type_id in prints_free:
                prints_free[t.type_id] = min(caps[t.type_id], prints_free[t.type_id] + 1)
            running.remove(t)
        for tid in by_type:
            if tid not in completed and all(x.task_id in started and x.end <= now for x in by_type[tid]):
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
