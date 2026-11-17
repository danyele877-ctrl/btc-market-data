from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TrackingWorkflowPersistenceTests(unittest.TestCase):
    def test_bridge_workflow_persists_prediction_tracking_archive(self):
        workflow = (ROOT / ".github/workflows/render-bridge.yml").read_text(encoding="utf-8")
        self.assertIn("cp data/zec_prediction_tracking.json /tmp/zec-prediction-tracking.json", workflow)
        self.assertIn("cp /tmp/zec-prediction-tracking.json data/zec_prediction_tracking.json", workflow)
        self.assertRegex(workflow, r"git add[^\n]*data/zec_prediction_tracking\.json")

    def test_tracking_code_changes_trigger_bridge_and_pull_request_tests(self):
        bridge = (ROOT / ".github/workflows/render-bridge.yml").read_text(encoding="utf-8")
        tests = (ROOT / ".github/workflows/zec-predictive-engine-tests.yml").read_text(encoding="utf-8")
        self.assertIn('"zec_prediction_tracking.py"', bridge)
        self.assertIn('"zec_prediction_tracking.py"', tests)
        self.assertIn('"zec_shadow_signal.py"', tests)


if __name__ == "__main__":
    unittest.main()
