"""Recurring progress must follow ESI job identity, never reuse completed batch rows."""
import json
import sqlite3
import sys
import unittest
from contextlib import ExitStack
from unittest.mock import patch
sys.path.insert(0, '.')
from app.reactions import jobs as J
from app.reactions import orders as O


class Connection:
    def __init__(self, con): self.con = con
    def __getattr__(self, name): return getattr(self.con, name)
    def close(self): pass


class RecurringIdentityTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          CREATE TABLE pp_characters (character_id INTEGER PRIMARY KEY, context_id INTEGER,
            mass_reactions INTEGER, advanced_mass_reactions INTEGER);
          CREATE TABLE pp_char_industry_jobs (character_id INTEGER, jobs_json TEXT);
          CREATE TABLE pp_reaction_orders (id INTEGER PRIMARY KEY, context_id INTEGER,
            priority INTEGER, top_level_runs INTEGER, assigned_runs INTEGER, recurring_interval_days REAL,
            recurring_next_at REAL DEFAULT 100, recurring_error TEXT, status TEXT DEFAULT 'open');
          CREATE TABLE pp_reaction_assignments (id INTEGER PRIMARY KEY, character_id INTEGER,
            type_id INTEGER, name TEXT DEFAULT 'RCF', runs INTEGER, input_cost REAL DEFAULT 0,
            reward REAL DEFAULT 0, tier_order INTEGER, order_id INTEGER, created_at REAL,
            esi_job_id INTEGER, last_completed_at REAL, slot_deferred INTEGER DEFAULT 0,
            cadence_over_h REAL DEFAULT 0, surplus_runs INTEGER DEFAULT 0,
            jobs_saved INTEGER DEFAULT 0, recover_runs INTEGER DEFAULT 0);
          INSERT INTO pp_characters VALUES (1, 1, 5, 4);
          INSERT INTO pp_reaction_orders (id,context_id,priority,top_level_runs,assigned_runs,recurring_interval_days) VALUES (46, 1, 0, 1000, 1000, 7);
        ''')
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.addCleanup(self.db.close)
        self.stack.enter_context(patch.object(J, 'get_connection', return_value=Connection(self.db)))
        for name in ('ensure_industry_jobs_table', 'ensure_reaction_assignments_table',
                     'ensure_reaction_orders_table', '_invalidate_dashboard_cache'):
            self.stack.enter_context(patch.object(J, name))
        self.stack.enter_context(patch.object(J, 'reaction_manual_marks', return_value={}))

    def row(self, rid, generation, runs=111, job=None, done=None, stage=0, tid=57457):
        self.db.execute('INSERT INTO pp_reaction_assignments '
                        '(id,character_id,type_id,runs,tier_order,order_id,created_at,esi_job_id,last_completed_at) '
                        'VALUES (?,1,?,?,?,46,?,?,?)', (rid, tid, runs, stage, generation, job, done))
        self.db.commit()

    def snapshot(self, jobs):
        self.db.execute('DELETE FROM pp_char_industry_jobs')
        self.db.execute('INSERT INTO pp_char_industry_jobs VALUES (1,?)', (json.dumps(jobs),))
        self.db.commit()

    def job(self, jid, runs=111, status='active', tid=57457):
        return {'job_id': jid, 'product_type_id': tid, 'runs': runs, 'status': status,
                'start_date': '2026-09-20T12:00:00Z', 'end_date': '2099-09-27T12:00:00Z'}

    def rows(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM pp_reaction_assignments ORDER BY id')]

    def test_new_job_does_not_reopen_completed_order_cycle(self):
        self.row(1, 100, done=150)
        self.row(2, 200)
        self.snapshot([self.job(10)])
        J.bind_reaction_jobs_to_plan(1)
        old, current = self.rows()
        self.assertEqual(old['last_completed_at'], 150)
        self.assertIsNone(old['esi_job_id'])
        self.assertEqual(current['esi_job_id'], 10)

    def test_finished_bound_feeder_unlocks_its_own_next_stage(self):
        self.row(1, 100, job=10, tid=57453)
        self.row(2, 100, stage=1)
        self.snapshot([self.job(10, status='ready', tid=57453), self.job(11)])
        J.bind_reaction_jobs_to_plan(1)
        feeder, top = self.rows()
        self.assertIsNotNone(feeder['last_completed_at'])
        self.assertEqual(top['esi_job_id'], 11)

    def test_unfinished_feeder_still_blocks_other_generation_jobs(self):
        self.row(1, 100, job=10, tid=57453)
        self.row(2, 100, stage=1)
        self.snapshot([self.job(10, tid=57453), self.job(11)])
        J.bind_reaction_jobs_to_plan(1)
        self.assertIsNone(self.rows()[1]['esi_job_id'])

    def test_legacy_wrong_binding_cannot_complete_a_future_stage(self):
        self.row(1, 100, job=10, tid=57453)
        self.row(2, 100, stage=1, job=11)
        self.snapshot([self.job(10, tid=57453), self.job(11, status='ready')])
        J.bind_reaction_jobs_to_plan(1)
        top = self.rows()[1]
        self.assertIsNone(top['esi_job_id'])
        self.assertIsNone(top['last_completed_at'])

    def test_levelling_uses_bound_ids_and_leaves_future_batch_alone(self):
        self.row(1, 100, runs=111, job=10)
        self.row(2, 100, runs=124, job=11)
        self.row(3, 200, runs=112)
        self.row(4, 200, runs=111)
        # Reverse ESI order and include leftover surplus. Neither may rewrite a future batch.
        self.snapshot([self.job(99, 200), self.job(11, 111), self.job(10, 124)])
        J.level_product_runs(1)
        self.assertEqual([r['runs'] for r in self.rows()], [124, 111, 112, 111])
        J.level_product_runs(1)
        self.assertEqual([r['runs'] for r in self.rows()], [124, 111, 112, 111])

    def test_waiting_pipeline_is_not_a_failed_assignment(self):
        for reason in ('waiting', 'backlog'):
            self.db.execute("UPDATE pp_reaction_orders SET recurring_error='old failure'")
            self.db.commit()
            with patch.object(O, 'get_connection', return_value=Connection(self.db)), \
                 patch.object(O, 'reaction_capacity_snapshot_fresh', return_value=(True, None)), \
                 patch.object(O, 'clone_recurring_cycle', return_value={'released': False, reason: True}):
                result = O._release_recurring_cycle(46, 1)
            self.assertIsNone(result['order']['recurring_error'])
            self.assertEqual(result['order']['recurring_interval_days'], 7)
            self.assertEqual(result['order']['recurring_next_at'], 100)
            self.assertFalse(result['released'])

    def test_surplus_job_cannot_report_a_future_order_stage_as_running(self):
        self.row(1, 100, job=10, tid=57453)
        self.row(2, 100, stage=1)
        stages = J.chain_stage_state(self.rows(), [self.job(10, tid=57453), self.job(99, 200)], 1)
        top = next(s for s in stages if s['stage'] == 1)
        self.assertEqual(top['running'], 0)
        self.assertEqual(top['todo'], 1)
        self.assertFalse(top['ready'])

    def test_real_assignment_failure_still_has_an_actionable_error(self):
        with patch.object(O, 'get_connection', return_value=Connection(self.db)), \
             patch.object(O, 'reaction_capacity_snapshot_fresh', return_value=(True, None)), \
             patch.object(O, 'clone_recurring_cycle', return_value={'released': False, 'error': 'No free slots'}):
            result = O._release_recurring_cycle(46, 1)
        self.assertEqual(result['order']['recurring_error'], 'No free slots')


if __name__ == '__main__': unittest.main()
