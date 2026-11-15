import unittest
from datetime import datetime, timedelta, timezone

from zec_queue_model import INSTRUMENT_ID, estimate_queue_depletion


BASE = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
ENTRY = int(BASE.timestamp() * 1000)


def history():
    books = []
    for second in range(301):
        books.append({
            "ts_ms": ENTRY + second * 1000,
            "source": "websocket",
            "seq_id": 1000 + second,
            "bids": [["100.00", "5"], ["99.99", "10"]],
            "asks": [["100.20", "10"], ["100.21", "8"]],
        })
    captures = []
    for minute in range(6):
        captures.append({
            "captured_at": (BASE + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z"),
            "trade_events_returned": 10, "trade_events_truncated": False,
            "trade_gap_count": 0, "book_gap_count": 0, "book_valid": True,
            "trade_age_seconds": 1, "book_age_seconds": 1,
        })
    return {
        "instrument": INSTRUMENT_ID,
        "book_snapshots": books,
        "capture_status": captures,
        "trades": [
            {"tradeId": "1", "ts_ms": ENTRY+10000, "side": "sell", "sz": "6",
             "px": "100.00", "source": "websocket", "gap_before": False},
            {"tradeId": "2", "ts_ms": ENTRY+20000, "side": "sell", "sz": "1",
             "px": "99.99", "source": "websocket", "gap_before": False},
        ],
    }


class QueueDepletionTests(unittest.TestCase):
    def test_buy_limit_queue_is_depleted_by_aggressive_sell_volume(self):
        result = estimate_queue_depletion(history(), ENTRY, 100.0, "LONG", 0.01)
        self.assertEqual(result["status"], "STIMA QUEUE DISPONIBILE")
        self.assertEqual(result["queue_ahead_contracts"], 5.0)
        self.assertEqual(result["aggressive_volume_at_or_better_contracts"], 7.0)
        self.assertTrue(result["queue_depleted_proxy"])

    def test_sell_limit_uses_aggressive_buy_volume(self):
        value = history()
        value["book_snapshots"] = [
            {**row, "bids": [["99.80", "10"]], "asks": [["100.00", "4"]]}
            for row in value["book_snapshots"]
        ]
        value["trades"] = [{
            "tradeId": "3", "ts_ms": ENTRY+10000, "side": "buy",
            "sz": "6", "px": "100.00", "source": "websocket", "gap_before": False,
        }]
        result = estimate_queue_depletion(value, ENTRY, 100.0, "SHORT", 0.01)
        self.assertEqual(result["status"], "STIMA QUEUE DISPONIBILE")
        self.assertEqual(result["queue_ahead_contracts"], 4.0)
        self.assertTrue(result["queue_depleted_proxy"])

    def test_limit_outside_visible_top_ten_depth_is_not_estimated(self):
        result = estimate_queue_depletion(history(), ENTRY, 99.50, "LONG", 0.01)
        self.assertEqual(result["status"], "LIMIT FUORI PROFONDITA VISIBILE")

    def test_trade_gap_blocks_queue_estimate(self):
        value = history()
        value["trades"][0]["gap_before"] = True
        result = estimate_queue_depletion(value, ENTRY, 100.0, "LONG", 0.01)
        self.assertEqual(result["status"], "GAP TRADE NELLA FINESTRA")

    def test_marketable_limit_is_not_misclassified_as_passive_queue(self):
        result = estimate_queue_depletion(history(), ENTRY, 100.20, "LONG", 0.01)
        self.assertEqual(result["status"], "LIMIT MARKETABILE")

    def test_missing_entry_book_blocks_estimate(self):
        value = history()
        value["book_snapshots"] = [row for row in value["book_snapshots"] if row["ts_ms"] != ENTRY]
        result = estimate_queue_depletion(value, ENTRY, 100.0, "LONG", 0.01)
        self.assertEqual(result["status"], "BOOK ENTRY NON DISPONIBILE")


if __name__ == "__main__":
    unittest.main()
