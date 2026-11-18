import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from render_bridge_publication import (
    _microstructure_problems,
    install_json_atomically,
    publication_decision,
)


def pair(captured_at, *, feed_status="ok", snapshot_status="ok", snapshot_at=None):
    return (
        {"status": feed_status, "captured_at": captured_at},
        {
            "source_status": snapshot_status,
            "source_captured_at": snapshot_at or captured_at,
            "generated_at": captured_at,
        },
    )


class RenderBridgePublicationTests(unittest.TestCase):
    def test_newer_paired_candidate_can_publish(self):
        current = pair("2026-11-16T14:00:00Z")
        candidate = pair("2026-11-16T14:01:00Z")
        self.assertEqual(publication_decision(*candidate, *current)[0], True)

    def test_older_candidate_cannot_overwrite_newer_published_pair(self):
        published = pair("2026-11-16T14:02:00Z")
        candidate = pair("2026-11-16T14:01:00Z")
        publish, reason = publication_decision(*candidate, *published)
        self.assertFalse(publish)
        self.assertIn("older", reason)

    def test_snapshot_watermark_alone_blocks_older_candidate(self):
        published = pair("2026-11-16T14:00:00Z", snapshot_at="2026-11-16T14:02:00Z")
        candidate = pair("2026-11-16T14:01:00Z")
        self.assertFalse(publication_decision(*candidate, *published)[0])

    def test_empty_or_invalid_existing_file_does_not_block_repair(self):
        candidate = pair("2026-11-16T14:01:00Z")
        self.assertTrue(publication_decision(*candidate, None, None)[0])

    def test_equal_timestamp_can_republish_changed_snapshot(self):
        published = pair("2026-11-16T14:01:00Z")
        candidate = pair("2026-11-16T14:01:00Z")
        self.assertTrue(publication_decision(*candidate, *published)[0])

    def test_mismatched_candidate_pair_is_rejected(self):
        feed, snapshot = pair("2026-11-16T14:01:00Z", snapshot_at="2026-11-16T14:00:00Z")
        publish, reason = publication_decision(feed, snapshot, None, None)
        self.assertFalse(publish)
        self.assertIn("do not match", reason)

    def test_non_ok_candidate_is_rejected(self):
        feed, snapshot = pair("2026-11-16T14:01:00Z", feed_status="error")
        self.assertFalse(publication_decision(feed, snapshot, None, None)[0])


class PublicationBundleSafetyTests(unittest.TestCase):
    captured_at = "2026-11-18T11:00:00Z"

    def valid_microstructure(self):
        captured_ms = int(datetime.fromisoformat(self.captured_at.replace("Z", "+00:00")).timestamp() * 1000)
        return {
            "schema_version": 1,
            "instrument": "ZEC-USD_UM_XPERP-310530",
            "trades": [{
                "tradeId": "123", "ts_ms": captured_ms - 1000, "side": "buy",
                "sz": "2", "px": "900", "source": "websocket",
            }],
            "book_snapshots": [{
                "ts_ms": captured_ms - 500, "source": "rest",
                "bids": [["899", "3"]], "asks": [["901", "4"]],
            }],
            "open_interest_history": [],
            "capture_status": [{"captured_at": "2026-11-18T10:59:30Z", "book_valid": True}],
        }

    def valid_feed(self, *, trades=1, books=1):
        return {
            "captured_at": self.captured_at,
            "microstructure_history": {
                "unique_trades": trades,
                "book_snapshots": books,
                "websocket_trades": trades,
                "websocket_book_snapshots": 0,
            },
        }

    def test_microstructure_requires_real_ordered_source_records(self):
        problems = _microstructure_problems(
            self.valid_microstructure(), self.valid_feed(),
            datetime.fromisoformat(self.captured_at.replace("Z", "+00:00")),
        )
        self.assertEqual(problems, [])

    def test_empty_microstructure_cannot_replace_existing_history(self):
        micro = self.valid_microstructure()
        micro.update({"trades": [], "book_snapshots": [], "open_interest_history": []})
        feed = self.valid_feed(trades=0, books=0)
        problems = _microstructure_problems(
            micro, feed, datetime.fromisoformat(self.captured_at.replace("Z", "+00:00")),
        )
        self.assertTrue(any("no acquired market records" in item for item in problems))

    def test_feed_history_counts_must_match_microstructure_generation(self):
        problems = _microstructure_problems(
            self.valid_microstructure(), self.valid_feed(trades=2),
            datetime.fromisoformat(self.captured_at.replace("Z", "+00:00")),
        )
        self.assertTrue(any("generation mismatch for unique_trades" in item for item in problems))

    def test_atomic_install_preserves_previous_json_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "candidate.json"
            target = Path(directory) / "published.json"
            source.write_text(json.dumps({"new": True}), encoding="utf-8")
            target.write_text(json.dumps({"old": True}), encoding="utf-8")

            with patch("render_bridge_publication.os.replace", side_effect=OSError("simulated failure")):
                with self.assertRaises(OSError):
                    install_json_atomically(source, target)

            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"old": True})
            self.assertEqual(list(Path(directory).glob(".published.json.*.tmp")), [])

    def test_atomic_install_rejects_invalid_candidate_before_replacing(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "candidate.json"
            target = Path(directory) / "published.json"
            source.write_text("", encoding="utf-8")
            target.write_text(json.dumps({"old": True}), encoding="utf-8")

            with self.assertRaises(ValueError):
                install_json_atomically(source, target)

            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"old": True})


if __name__ == "__main__":
    unittest.main()
