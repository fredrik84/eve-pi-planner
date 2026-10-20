"""Stopped extraction must not refill launchpads in the dashboard or alert engine.

Run in the application container: python3 tests/test_storage_projection.py
"""
import json
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, ".")
from app.pi_sim import project_storage
from app.alerts import compute_alerts
from app.alert_settings import DEFAULTS

T0 = 1_700_000_000.0
HOUR = 3600


class StorageProjectionTests(unittest.TestCase):
    def project(self, *, base=0, rate=100, expiry=T0 + 2 * HOUR, anchor=T0, now=T0 + 4 * HOUR):
        return project_storage({"vol_m3": base, "cap_m3": 10000, "fill_m3_h": rate},
                               {"expiry": expiry}, anchor, now)

    def test_stopped_empty_launchpad_stays_empty_after_rescan(self):
        p = self.project(expiry=T0 - HOUR, now=T0 + 7 * 24 * HOUR)
        self.assertEqual(p["vol_m3"], 0)
        self.assertEqual(p["fill_m3_h"], 0)
        self.assertIsNone(p["hours_until_full"])

    def test_production_up_to_expiry_is_preserved_and_then_stops(self):
        self.assertEqual(self.project(base=100)["vol_m3"], 300)
        self.assertEqual(self.project(base=100, now=T0 + 1000 * HOUR)["vol_m3"], 300)

    def test_active_colony_projects_normally(self):
        p = self.project(base=8000, expiry=T0 + 50 * HOUR)
        self.assertEqual(p["vol_m3"], 8400)
        self.assertEqual(p["hours_until_full"], 16)

    def test_no_full_deadline_when_program_ends_before_pad_fills(self):
        p = self.project(base=8000, expiry=T0 + 10 * HOUR)
        self.assertEqual(p["vol_m3"], 8400)
        self.assertIsNone(p["hours_until_full"])

    def test_real_full_pad_remains_full_after_extraction_stops(self):
        self.assertEqual(self.project(base=10000)["vol_m3"], 10000)

    def test_no_projection_without_checkpoint_or_backwards_in_time(self):
        self.assertEqual(self.project(base=100, anchor=None)["vol_m3"], 100)
        self.assertEqual(self.project(base=100, anchor=T0 + 10 * HOUR)["vol_m3"], 100)

    def test_legacy_snapshot_without_expiry_still_projects(self):
        self.assertEqual(self.project(expiry=None)["vol_m3"], 400)

    def test_alert_engine_does_not_report_phantom_storage_full(self):
        row = {"cid": 1, "ch": "Stopped alt", "system": "Test", "pn": 1, "planet_id": 1,
               "issues": None, "is_ext": 1, "checkpoint_at": T0, "scanned_at": T0,
               "sim_state": json.dumps({"expiry": T0 - HOUR}),
               "storage": json.dumps({"vol_m3": 0, "cap_m3": 10000, "fill_m3_h": 100})}
        with patch('app.alerts.get_alert_settings', return_value=DEFAULTS), \
             patch('app.alerts._fetch_factory_refill_hours', return_value=None), \
             patch('app.alerts._reaction_alerts', return_value=[]):
            alerts = compute_alerts(1, rows=[row], now=T0 + 7 * 24 * HOUR)
            self.assertTrue(any(a['kind'] == 'expired' for a in alerts))
            self.assertFalse(any(a['kind'] == 'storage_full' for a in alerts))
            row['storage'] = json.dumps({"vol_m3": 10000, "cap_m3": 10000, "fill_m3_h": 100})
            alerts = compute_alerts(1, rows=[row], now=T0 + 7 * 24 * HOUR)
            self.assertTrue(any(a['kind'] == 'storage_full' for a in alerts))


class TokenInvalidationTests(unittest.TestCase):
    def test_revoked_token_invalidates_character_list_immediately(self):
        from app import esi
        con = MagicMock()
        con.execute.return_value.fetchone.return_value = {"context_id": 42}
        with patch.object(esi, 'get_connection', return_value=con), \
             patch.object(esi.httpx, 'Client') as client, \
             patch.object(esi, 'cache_invalidate') as invalidate:
            client.return_value.__enter__.return_value.post.return_value.status_code = 400
            self.assertIsNone(esi._refresh_token(7, 'revoked'))
            self.assertIn("refresh_token=''", con.execute.call_args.args[0])
            con.commit.assert_called_once()
            invalidate.assert_called_once_with(esi.charlist_key(42))


if __name__ == '__main__':
    unittest.main()
