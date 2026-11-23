import unittest
from datetime import datetime, timedelta, timezone

from zec_impulse_shadow import BTC_ID, ZEC_STREAM, first_impulse


class ImpulseTests(unittest.TestCase):
    def test_aligned_fresh_closes_only_and_suppress_repeated_rally(self):
        now = datetime(2026, 11, 23, 13, 56, tzinfo=timezone.utc)
        instruments = {}
        for key in (BTC_ID, ZEC_STREAM):
            rows = [{"completed_at": (now + timedelta(minutes=i)).isoformat(),
                     "open": 100, "close": 100, "volume_quote": 100}
                    for i in range(-40, 1)]
            rows[-1].update(close=101, volume_quote=500)
            instruments[key] = {"1m": rows}
        self.assertEqual(first_impulse(instruments, now)["side"], "LONG")
        self.assertIsNone(first_impulse(instruments, now + timedelta(seconds=45)))
        for item in instruments.values():
            item["1m"].append({**item["1m"][-1], "completed_at": (now + timedelta(minutes=1)).isoformat()})
        self.assertIsNone(first_impulse(instruments, now + timedelta(minutes=1)))
        instruments[ZEC_STREAM]["1m"].pop(-3)
        self.assertIsNone(first_impulse(instruments, now))
