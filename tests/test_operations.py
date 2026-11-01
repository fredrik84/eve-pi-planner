"""Real failure paths and aggregate operational telemetry without network or identity leakage."""
import contextlib
import json
import sqlite3
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

import httpx

sys.path.insert(0, '.')
from app import cache, db, esi, esi_http, operations, profiling


class OperationsTests(unittest.TestCase):
    def test_events_are_fixed_categories_and_collection_cannot_break_work(self):
        with patch.object(profiling, 'profiling_enabled', return_value=True), self.assertLogs('evepi.access', 'INFO') as logs:
            operations.note('planet_scan', 'partial', fetched=2, skipped=1, failed=1)
            operations.note('character-999-secret', 'failed')
            operations.note('planet_scan', 'private-error-text')
            operations.note('planet_scan', 'failed', reason='https://secret/999')
            operations.note('planet_scan', 'failed', fetched=float('nan'))
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(json.loads(logs.records[0].message), dict(event='operation', operation='planet_scan',
                         outcome='partial', reason='none', fetched=2, skipped=1, failed=1))
        with patch.object(profiling, 'profiling_enabled', return_value=False), patch.object(operations.log, 'info') as write:
            operations.note('planet_scan', 'success')
            write.assert_not_called()
        with patch.object(profiling, 'profiling_enabled', side_effect=RuntimeError('private')):
            operations.note('planet_scan', 'success')  # Logging failure is invisible to callers.

    def test_upstream_failures_keep_request_semantics_and_never_log_urls_or_tokens(self):
        client = Mock()
        request = httpx.Request('GET', 'https://esi.invalid/characters/999/planets/', headers={'Authorization':'Bearer private-token'})
        response = httpx.Response(503, request=request)
        with patch.object(esi_http, '_pre_request_wait'), patch.object(esi_http, '_record'), \
                patch.object(profiling, 'profiling_enabled', return_value=True), self.assertLogs('evepi.access', 'INFO') as logs:
            client.request.return_value = response
            self.assertIs(esi_http.get(str(request.url), client=client, token='private-token'), response)
            client.request.side_effect = httpx.ReadTimeout('private-timeout-url', request=request)
            with self.assertRaises(httpx.ReadTimeout):
                esi_http.get(str(request.url), client=client, token='private-token')
        events = [json.loads(x.message) for x in logs.records]
        self.assertEqual([x['reason'] for x in events], ['upstream','timeout'])
        self.assertTrue(all(x['outcome']=='failed' for x in events))
        self.assertNotIn('private', str(events))
        self.assertNotIn('999', str(events))

    def test_failed_planet_list_skills_and_valid_unaffiliated_character_are_distinguished(self):
        with patch.object(esi.esi_http, 'get', side_effect=httpx.ReadTimeout('secret')), patch.object(esi, 'note_operation') as note:
            self.assertEqual(esi._fetch_planets(999, 'private'), dict(fetched=0, skipped=0, failed=0))
            note.assert_called_once_with('planet_scan', 'failed', reason='timeout', fetched=0, skipped=0, failed=0)
        response = httpx.Response(503, request=httpx.Request('GET','https://esi.invalid/private/999'))
        with patch.object(esi.esi_http, 'get', return_value=response), patch.object(esi, 'note_operation') as note:
            self.assertEqual(esi._fetch_skills(999,'private'), {})
            note.assert_called_once_with('skills_refresh','failed',reason='upstream')
        response = httpx.Response(200, json={}, request=httpx.Request('GET','https://esi.invalid/private/999'))
        with patch.object(esi.esi_http, 'get', return_value=response), patch.object(esi, 'note_operation') as note:
            self.assertIsNone(esi._fetch_alliance_id(999))
            note.assert_called_once_with('alliance_refresh','success')

    def test_sso_rejection_clears_token_but_transient_failure_preserves_it(self):
        con=sqlite3.connect(':memory:'); con.row_factory=sqlite3.Row
        self.addCleanup(con.close)
        con.execute('CREATE TABLE pp_characters(character_id int,context_id int,refresh_token text,access_token text,token_expiry text)')
        con.execute("INSERT INTO pp_characters VALUES (999,7,'private-refresh','old-access','expired')")
        class Borrowed:
            def close(self): pass
            def __getattr__(self,name):return getattr(con,name)
        for status,reason in [(503,'upstream'),(400,'auth_rejected'),(200,'none')]:
            with self.subTest(status=status):
                con.execute("UPDATE pp_characters SET refresh_token='private-refresh'"); con.commit()
                client=MagicMock(); client.__enter__.return_value=client
                client.post.return_value=httpx.Response(status, json={'access_token':'new-private','refresh_token':'new-refresh'},
                                                       request=httpx.Request('POST','https://sso.invalid/private'))
                with patch.object(esi.httpx,'Client',return_value=client), patch.object(esi,'get_connection',return_value=Borrowed()), \
                        patch.object(esi,'cache_invalidate'), patch.object(esi,'note_operation') as note:
                    token=esi._refresh_token(999,'private-refresh')
                stored=con.execute('SELECT refresh_token FROM pp_characters').fetchone()[0]
                if status==503:
                    self.assertIsNone(token); self.assertEqual(stored,'private-refresh')
                    note.assert_called_once_with('token_refresh','failed',reason=reason)
                elif status==400:
                    self.assertIsNone(token); self.assertEqual(stored,'')
                    note.assert_called_once_with('token_refresh','reauthorization_required',reason=reason)
                else:
                    self.assertEqual(token,'new-private'); self.assertEqual(stored,'new-refresh')
                    note.assert_called_once_with('token_refresh','success')

    def test_metrics_use_numeric_global_counts_and_fixed_job_labels(self):
        con=sqlite3.connect(':memory:');con.row_factory=sqlite3.Row
        self.addCleanup(con.close)
        con.executescript('''
            CREATE TABLE pp_characters(is_dummy int,refresh_token text,scan_failed_at real,token_expiry text);
            INSERT INTO pp_characters VALUES (0,'private-refresh',NULL,'expired'),(0,'',10,'expired'),(1,'',10,'expired'),(0,'live',0,'expired');
            CREATE TABLE pp_job_runs(id int,job text,started_at real,ended_at real,status text,error text);
            INSERT INTO pp_job_runs VALUES (1,'notify_check',100,110,'ok','secret'),(2,'notify_check',200,220,'error','character-999'),
                (3,'notify_check',300,NULL,'running','private'),(4,'character-999',400,450,'error','private'),(5,'notify_check',-100000,-99900,'error','old');
            CREATE TABLE pp_job_leases(job text,lease_until real);
            INSERT INTO pp_job_leases VALUES ('notify_check',1100);
            CREATE TABLE pp_job_config(job text,enabled int);
            INSERT INTO pp_job_config VALUES ('notify_check',0);
        ''')
        store={}
        with patch.object(profiling,'profiling_enabled',return_value=True), patch.object(db,'get_connection',side_effect=lambda:contextlib.nullcontext(con)) as connect, \
                patch.object(cache,'cache_get_json',side_effect=store.get), \
                patch.object(cache,'cache_set_json',side_effect=lambda key,value,ttl:store.__setitem__(key,value)), patch.object(operations.time,'time',return_value=1000):
            lines=operations.prometheus_lines()
            self.assertEqual(operations.prometheus_lines(),lines)
            self.assertEqual(connect.call_count,1)
        text='\n'.join(lines)
        for value in ['evepi_characters_reauthorization_required 1.0','evepi_characters_scan_failed 1.0',
                      'evepi_job_runs_total{task="notify_check"} 3.0','evepi_job_failures_total{task="notify_check"} 2.0',
                      'evepi_job_failures_24h{task="notify_check"} 1.0',
                      'evepi_job_unfinished_runs{task="notify_check"} 1.0','evepi_job_lease_active{task="notify_check"} 1.0',
                      'evepi_job_enabled{task="notify_check"} 0.0','evepi_job_runs_total{task="yield_aggregate"} 0.0',
                      '# TYPE evepi_job_failures_total counter','evepi_operations_available 1.0']:
            self.assertIn(value,text)
        self.assertNotIn('private',text);self.assertNotIn('character-999',text)
        self.assertNotIn('evepi_job_last_duration_seconds{task="notify_check"}',text)  # Latest run isn't complete.
        self.assertEqual(text.count('# TYPE evepi_job_failures_total counter'),1)
        with patch.object(profiling,'profiling_enabled',return_value=True), patch.object(cache,'cache_get_json',return_value=None), \
                patch.object(cache,'cache_set_json'),patch.object(db,'get_connection',side_effect=RuntimeError('private')):
            text='\n'.join(operations.prometheus_lines())
            self.assertIn('evepi_operations_available 0.0',text)
            self.assertNotIn('private',text)


if __name__=='__main__': unittest.main()
