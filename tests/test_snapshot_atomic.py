import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from build_radar_snapshot import write_json_atomically


class AtomicRadarSnapshotTests(unittest.TestCase):
    def test_complete_json_replaces_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "radar_snapshot.json"
            target.write_text('{"previous":true}', encoding="utf-8")

            write_json_atomically(target, {"status": "ok", "value": 42})

            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")),
                {"status": "ok", "value": 42},
            )
            self.assertEqual(list(Path(directory).glob(".radar_snapshot.json.*.tmp")), [])

    def test_failed_replace_preserves_last_known_good_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "radar_snapshot.json"
            target.write_text('{"previous":true}', encoding="utf-8")

            with patch("build_radar_snapshot.os.replace", side_effect=OSError("simulated failure")):
                with self.assertRaises(OSError):
                    write_json_atomically(target, {"status": "new"})

            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")),
                {"previous": True},
            )
            self.assertEqual(list(Path(directory).glob(".radar_snapshot.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
