import importlib.util
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "tools" / "backfill_git_candles.py"
SPEC = importlib.util.spec_from_file_location("backfill_git_candles", MODULE)
BACKFILL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BACKFILL)


class CandleValidationTests(unittest.TestCase):
    def setUp(self):
        self.source = {"path": "fixture.json", "commit": "a" * 40, "blob_sha": "b" * 40}

    def test_accepts_aligned_closed_ohlc(self):
        row = {
            "ts_ms": 1794882180000,
            "completed_at": "2026-11-17T02:24:00Z",
            "open": "10", "high": "12", "low": "9", "close": "11", "volume": "3",
        }
        result = BACKFILL.validate_candle(row, "ZEC-USD_UM_XPERP", "1m", self.source)
        self.assertIsNotNone(result)
        self.assertEqual(result["close"], 11.0)
        self.assertEqual(result["provenance"][0]["commit"], "a" * 40)

    def test_rejects_bad_ohlc(self):
        row = {"ts_ms": 1794882180000, "open": 10, "high": 8, "low": 9, "close": 11}
        self.assertIsNone(BACKFILL.validate_candle(row, "ZEC-USD_UM_XPERP", "1m", self.source))

    def test_rejects_misaligned_timestamp(self):
        row = {"ts_ms": 1794882180123, "open": 10, "high": 12, "low": 9, "close": 11}
        self.assertIsNone(BACKFILL.validate_candle(row, "ZEC-USD_UM_XPERP", "1m", self.source))

    def test_rejects_mismatched_completion_time(self):
        row = {
            "ts_ms": 1794882180000, "completed_at": "2026-11-17T02:23:00Z",
            "open": 10, "high": 12, "low": 9, "close": 11,
        }
        self.assertIsNone(BACKFILL.validate_candle(row, "ZEC-USD_UM_XPERP", "1m", self.source))


if __name__ == "__main__":
    unittest.main()
