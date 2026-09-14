"""Slot reuse and order allocation regressions; run in the local application container."""
import sys
import random
import unittest
from collections import defaultdict
from contextlib import ExitStack
from unittest.mock import Mock, patch

sys.path.insert(0, ".")

from app.industry.schedule import Task, schedule
from app.reactions import jobs as reactions


class ReactionAllocationTests(unittest.TestCase):
    def allocate(self, capacity=2, pace="balanced", parallel=True, caps=None,
                 stages=(0,), runs=100, cadence=48, host_slots=None):
        hosts = [{"character_id": i + 1, "character_name": str(i + 1),
                  "slots": slots, "free_slots": slots}
                 for i, slots in enumerate(host_slots or [capacity])]

        def tiers(_inputs, share, _reached, _stock):
            return [(i + 10, {"runs": share, "cycle_time": 3600})
                    for i in range(len(stages))]

        with ExitStack() as stack:
            for name, value in {
                "_character_capacities": hosts, "reaction_stock_pool": {},
                "formula_concurrency_caps": caps or {}, "_reaction_cadence_hours": cadence,
                "_production_pace": pace, "_parallel_stages_on": parallel,
                "_tidy_runs_on": False, "_direct_tier_run_floors": {},
                "get_connection": Mock(), "tier_ranks": list(stages),
            }.items():
                stack.enter_context(patch.object(reactions, name, return_value=value))
            stack.enter_context(patch.object(reactions, "_ordered_chain_tiers", side_effect=tiers))
            insert = stack.enter_context(patch.object(reactions, "_insert_assignment_rows"))
            result = reactions._allocate_and_insert(
                1, 99, "Product", {"via": {"inputs": []}, "cycle_time": 3600},
                {}, {}, runs, 7)
        rows = [{"character": c.args[1], "type": c.args[2], "runs": c.args[4],
                 "jobs": c.args[5], "stage": c.args[8]} for c in insert.call_args_list]
        return result, rows

    def test_sequential_chain_fits_one_reactor(self):
        result, rows = self.allocate(capacity=1, stages=(0, 1))
        self.assertEqual(result["runs_assigned"], 100)
        self.assertEqual([r["jobs"] for r in rows], [1, 1, 1])

    def test_balanced_reuses_reactors_to_meet_each_stage_cadence(self):
        _, rows = self.allocate()
        self.assertEqual([r["jobs"] for r in rows], [2, 2])
        self.assertTrue(all((r["runs"] + r["jobs"] - 1) // r["jobs"] <= 51 for r in rows))

    def test_fastest_reuses_reactors_at_each_stage(self):
        _, rows = self.allocate(capacity=4, pace="fastest")
        self.assertEqual([r["jobs"] for r in rows], [4, 4])

    def test_siblings_share_capacity_but_later_stage_reuses_it(self):
        _, rows = self.allocate(capacity=4, stages=(0, 0))
        self.assertEqual([r["jobs"] for r in rows], [2, 2, 2])

    def test_legacy_flag_keeps_sum_budget(self):
        _, rows = self.allocate(capacity=4, stages=(0, 0), parallel=False)
        self.assertLessEqual(sum(r["jobs"] for r in rows), 4)

    def test_formula_limits_bind_across_hosts(self):
        result, rows = self.allocate(host_slots=[4, 2], pace="fastest", caps={10: 3, 99: 5})
        by_type = defaultdict(int)
        for row in rows:
            by_type[row["type"]] += row["jobs"]
        self.assertEqual(result["runs_assigned"], 100)
        self.assertLessEqual(by_type[10], 3)
        self.assertLessEqual(by_type[99], 5)
        self.assertEqual([c["runs"] for c in result["characters"]], [67, 33])

    def test_small_batch_uses_one_host(self):
        result, rows = self.allocate(host_slots=[10, 10, 5], runs=2)
        self.assertEqual(len(result["characters"]), 1)
        self.assertTrue(all(0 < r["jobs"] <= r["runs"] for r in rows))

    def test_stage_capacity_and_demand_survive_different_batch_sizes(self):
        for pace in ("balanced", "fastest"):
            for capacity in (2, 4, 10):
                for runs in (1, 17, 100, 501):
                    with self.subTest(pace=pace, capacity=capacity, runs=runs):
                        result, rows = self.allocate(capacity=capacity, pace=pace,
                                                     stages=(0, 0, 1), runs=runs)
                        self.assertEqual(result["runs_assigned"], runs)
                        load = defaultdict(int)
                        for row in rows:
                            self.assertEqual(row["runs"], runs)
                            self.assertTrue(0 < row["jobs"] <= runs)
                            load[row["stage"]] += row["jobs"]
                        self.assertLessEqual(max(load.values()), capacity)


class ManufacturingSchedulerTests(unittest.TestCase):
    def test_released_slot_is_reused_without_overlapping_running_job(self):
        tasks = [Task("short", 1, "manufacturing", 1, 1),
                 Task("long", 2, "manufacturing", 1, 10),
                 Task("next", 3, "manufacturing", 1, 2)]
        result = schedule(tasks, {t.type_id: [t] for t in tasks}, {},
                          {"manufacturing": 2}, {1: 3, 2: 2, 3: 1})
        self.assertEqual(result["unscheduled"], [])
        self.assertEqual(tasks[2].start, 1)
        self.assertEqual(tasks[2].slot, tasks[0].slot)
        self.assertNotEqual(tasks[2].slot, tasks[1].slot)

    def test_zero_duration_dependency_unlocks_consumer(self):
        tasks = [Task("instant", 1, "reaction", 1, 0),
                 Task("consumer", 2, "manufacturing", 1, 10)]
        result = schedule(tasks, {t.type_id: [t] for t in tasks}, {2: {1}},
                          {"manufacturing": 1, "reaction": 1}, {1: 2, 2: 1})
        self.assertEqual(result["unscheduled"], [])
        self.assertEqual(tasks[1].start, 0)
        self.assertEqual(tasks[1].end, 10)

    def test_one_print_serializes_jobs_across_orders(self):
        tasks = [Task("a", 1, "manufacturing", 1, 10, key=(1, 1)),
                 Task("b", 1, "manufacturing", 1, 10, key=(2, 1))]
        result = schedule(tasks, {t.sched_key(): [t] for t in tasks}, {},
                          {"manufacturing": 2}, {}, print_caps={1: 1})
        self.assertEqual(result["unscheduled"], [])
        self.assertEqual([t.start for t in tasks], [0, 10])

    def test_seeded_dags_respect_dependencies_slots_and_prints(self):
        rng = random.Random(42)
        for case in range(50):
            with self.subTest(case=case):
                tasks = [Task(str(i), i // 3, "reaction" if i // 3 % 2 else "manufacturing",
                              1, rng.randrange(20)) for i in range(30)]
                deps = {i: {j for j in range(i) if rng.random() < 0.1} for i in range(10)}
                pools = {"manufacturing": rng.randint(1, 5), "reaction": rng.randint(1, 5)}
                caps = {i: rng.randint(1, 3) for i in range(10)}
                by_type = defaultdict(list)
                for task in tasks:
                    by_type[task.type_id].append(task)
                result = schedule(tasks, by_type, deps, pools,
                                  {i: (rng.randrange(3), rng.random()) for i in range(10)}, caps)
                self.assertEqual(result["unscheduled"], [])
                for task in tasks:
                    self.assertTrue(0 <= task.slot < pools[task.activity])
                    for dep in deps[task.type_id]:
                        self.assertGreaterEqual(task.start, max(t.end for t in by_type[dep]))
                for now in {t.start for t in tasks}:
                    active = [t for t in tasks if t.start <= now < t.end]
                    self.assertEqual(len(active), len({(t.activity, t.slot) for t in active}))
                    for tid, cap in caps.items():
                        self.assertLessEqual(sum(t.type_id == tid for t in active), cap)


if __name__ == "__main__":
    unittest.main()
