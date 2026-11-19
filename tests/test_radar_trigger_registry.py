import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from radar_trigger_registry import record_trigger


class TriggerRegistryTests(unittest.TestCase):
    def document(self, now):
        return {
            "schema_version": 1, "captured_at": now.isoformat(),
            "generated_at": now.isoformat(),
            "feed": {"status": "ok", "fresh_at_publication": True,
                     "expires_at": (now + timedelta(seconds=60)).isoformat()},
            "quality": {"engine_ready": True, "required_prices_available": True},
            "decision": {"side": "LONG", "reason": "confirmed_trigger",
                         "invalidation_price": 10},
            "prices": {
                "BTC-USDC": {"last": 11},
                "ZEC-USDC": {"last": 11},
                "ZEC-USD_UM_XPERP-310530": {"last": 11, "mark": 11},
            },
        }

    def test_records_once_and_never_enables_orders(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            self.assertTrue(record_trigger(self.document(now), path, now))
            self.assertFalse(record_trigger(self.document(now), path, now))
            events = json.loads(path.read_text())["events"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["delivery_status"], "UNKNOWN")
            self.assertFalse(events[0]["orders_enabled"])

    def test_stale_and_no_signal_not_recorded(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            stale = self.document(now - timedelta(minutes=10))
            self.assertFalse(record_trigger(stale, path, now))
            neutral = self.document(now)
            neutral["decision"] = {"side": "NO_SIGNAL", "reason": "no_confirmed_trigger"}
            self.assertFalse(record_trigger(neutral, path, now))
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
