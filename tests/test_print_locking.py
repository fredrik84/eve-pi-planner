#!/usr/bin/env python3
"""A blueprint is an item: jobs running off one print never overlap in time.

A print is locked while a job is installed on it. The scheduler modelled runs and slots and never
this, so one owned BPO planned as many simultaneous jobs as there were free slots. Capping
concurrency inside one plan fixed it for a single batch, but a per-plan cap cannot see a second
plan — so with `industry_per_order_plans` two orders planned apart each scheduled jobs off the SAME
original (2f-residual #2).

`schedule()` now takes `print_caps` and treats a print exactly like a slot: a job needs a free slot
AND a free print to start, and the print is released when the job ends. That is what neither
approximation could express — a claim is permanent, whereas a print is merely BUSY, so it can be
handed to the next job the moment the first finishes. Correct and cheaper than serialising.

What is pinned:

  * with one print, no two jobs of that type are ever running at the same instant — across ORDERS,
    since the resource is keyed on the real `type_id` which is shared;
  * with N prints, at most N overlap;
  * the print is REUSED rather than spent — n jobs on one print still all get scheduled, they just
    queue;
  * a type absent from `print_caps` is unlimited, which is the old behaviour and the right default:
    a type we have observed nothing about must never be serialised on absent evidence.

In-process; run inside the container against a NON-PROD database.

    docker compose cp tests/test_print_locking.py web:/srv/app/tests/ && \
      docker compose exec web python3 tests/test_print_locking.py
"""
import sys

sys.path.insert(0, ".")

_fails = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


def _run(n_jobs: int, caps, pools=8, type_id=1000, keys=None):
    """Schedule `n_jobs` one-hour jobs of one type and return their (start, end) placements."""
    from app.industry.schedule import Task, schedule
    tasks = []
    for i in range(n_jobs):
        t = Task(task_id=f"t{i}", type_id=type_id, activity="manufacturing", runs=1,
                 duration=3600.0)
        # Distinct sched keys, as separately-planned orders produce — otherwise they would be one
        # batch and the question would not arise.
        t.key = (keys[i] if keys else i, type_id)
        tasks.append(t)
    by_key = {t.key: [t] for t in tasks}
    schedule(tasks, by_key, {}, {"manufacturing": pools}, {}, print_caps=caps)
    return [(t.start, t.end) for t in tasks]


def _max_overlap(spans) -> int:
    """The most jobs running at the same instant."""
    events = []
    for s, e in spans:
        events.append((s, 1)); events.append((e, -1))
    events.sort(key=lambda x: (x[0], x[1]))
    cur = best = 0
    for _, d in events:
        cur += d
        best = max(best, cur)
    return best


def main() -> int:
    print("\nONE print, eight free slots — the case that used to plan eight simultaneous jobs:")
    spans = _run(8, {1000: 1})
    check(_max_overlap(spans) == 1,
          f"never more than one job at a time (peak {_max_overlap(spans)})")
    check(len(spans) == 8 and all(e > 0 for _, e in spans),
          "...and all eight are still scheduled — the print is reused, not spent")
    check(max(e for _, e in spans) == 8 * 3600.0,
          f"...so they queue end to end (makespan {max(e for _, e in spans)/3600:.0f}h, want 8h)")

    print("\nthree prints:")
    spans = _run(9, {1000: 3})
    check(_max_overlap(spans) == 3, f"at most three overlap (peak {_max_overlap(spans)})")
    check(max(e for _, e in spans) == 3 * 3600.0, "9 jobs on 3 prints takes 3 hours")

    print("\nunknown ownership must NOT serialise — no cap is the old behaviour:")
    spans = _run(8, None)
    check(_max_overlap(spans) == 8, f"all eight run together (peak {_max_overlap(spans)})")
    spans = _run(8, {2222: 1})            # a cap on a DIFFERENT type
    check(_max_overlap(spans) == 8, "a cap on another type changes nothing")

    print("\nTHE CROSS-ORDER CASE: two orders, one original between them:")
    # Keys namespaced per order, exactly as plan_queue_per_order builds them. The print resource is
    # keyed on type_id, which is NOT namespaced — that is what makes the two orders contend.
    spans = _run(6, {1000: 1}, keys=[0, 0, 0, 1, 1, 1])
    check(_max_overlap(spans) == 1,
          f"two separately-planned orders still cannot both use the print (peak {_max_overlap(spans)})")
    check(len(set(s for s, _ in spans)) == 6,
          "...and every job gets its own start time rather than being dropped")

    print("\nthe print is handed on the instant a job ends, not held for the plan:")
    spans = sorted(_run(3, {1000: 1}))
    check(spans[1][0] == spans[0][1] and spans[2][0] == spans[1][1],
          f"each job starts exactly when the previous ended (got {[(s/3600, e/3600) for s, e in spans]})")

    guard_the_wiring()
    print("\n" + ("FAILED: " + "; ".join(_fails) if _fails else "all checks passed"))
    return 1 if _fails else 0


def guard_the_wiring() -> None:
    """`plan_queue_per_order` must actually HAND the caps to the scheduler.

    Everything above tests `schedule()` directly, which leaves the production wiring unpinned:
    deleting `print_caps=print_caps` from the per-order planner reintroduces the whole defect and
    every test in the repo stays green. Verified by doing exactly that. So this asserts the call
    site — structurally, because a behavioural test would need a full account fixture to reach it.

    It also pins that the caps are RECORDED, since a call passing a dict that is never filled would
    satisfy the first check and do nothing.
    """
    import ast
    import os
    print("\nthe per-order planner really hands the caps to the scheduler:")
    path = os.path.join("app", "industry", "schedule.py")
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "plan_queue_per_order"), None)
    check(fn is not None, "plan_queue_per_order still exists")
    if fn is None:
        return
    body = "\n".join(src.splitlines()[fn.lineno - 1:(fn.end_lineno or fn.lineno)])
    passes = any(
        isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) == "schedule" or getattr(n.func, "attr", None) == "schedule")
        and any(k.arg == "print_caps" for k in n.keywords)
        for n in ast.walk(ast.parse(body.strip().replace("def plan_queue_per_order", "def _f", 1)))
    ) if body else False
    check(passes or "print_caps=print_caps" in body,
          "it calls schedule(..., print_caps=...) — without this the cross-order fix is dead code")
    check("print_caps[" in body,
          "...and fills the caps from the plans, rather than handing over an empty dict")

    # The aggregated path must NOT get them: one batch already caps concurrency inside itself, and
    # passing caps there would be a behaviour change nobody asked for.
    agg = next((n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "plan_queue"), None)
    if agg is not None:
        agg_body = "\n".join(src.splitlines()[agg.lineno - 1:(agg.end_lineno or agg.lineno)])
        check("print_caps" not in agg_body,
              "the aggregated plan is left exactly as it was")


if __name__ == "__main__":
    sys.exit(main())
