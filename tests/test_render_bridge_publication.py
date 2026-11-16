import unittest

from render_bridge_publication import publication_decision


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


if __name__ == "__main__":
    unittest.main()
