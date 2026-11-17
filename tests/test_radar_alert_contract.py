import unittest

from radar_alert_contract import validate_snapshot


def valid_snapshot():
    safety = {
        "orders_enabled": False,
        "position_sizing_enabled": False,
        "sizing_allowed": False,
        "ctVal_reconciled": False,
        "ctVal_zec_per_contract": None,
        "margin_or_exposure_calculated": False,
        "max_risk_usdc": 15,
    }
    return {
        "generated_at": "2026-11-17T18:00:00Z",
        "source_status": "ok",
        "zec_early_signal": {
            "mode": "simulation_only",
            "decision": "ATTENDERE",
            "candidate_direction": "NEUTRA",
            "confidence": {"score": 50, "calibrated": False},
            "contract_safety": dict(safety),
            "execution": {
                "orders_enabled": False,
                "sizing_calculated": False,
                "position_quantity": None,
                "margin": None,
                "pnl": None,
            },
        },
        "zec_shadow_signal": {
            "mode": "diagnostic_only",
            "baseline_unchanged": True,
            "validation_status": "IN RACCOLTA",
            "orders_enabled": False,
            "position_sizing_enabled": False,
        },
        "zec_contract_safety": dict(safety),
        "zec_prediction_tracking": {
            "orders_enabled": False,
            "position_sizing_enabled": False,
        },
        "zec_predictive_engine": {
            "mode": "simulation_only",
            "orders_enabled": False,
            "position_sizing_enabled": False,
        },
    }


class RadarAlertContractTests(unittest.TestCase):
    def test_valid_snapshot_passes(self):
        self.assertEqual(validate_snapshot(valid_snapshot()), [])

    def test_missing_shadow_section_fails(self):
        snapshot = valid_snapshot()
        del snapshot["zec_shadow_signal"]
        self.assertTrue(any("zec_shadow_signal" in item for item in validate_snapshot(snapshot)))

    def test_orders_cannot_be_enabled(self):
        snapshot = valid_snapshot()
        snapshot["zec_shadow_signal"]["orders_enabled"] = True
        self.assertTrue(any("orders_enabled" in item for item in validate_snapshot(snapshot)))

    def test_unresolved_ctval_cannot_publish_contract_value(self):
        snapshot = valid_snapshot()
        snapshot["zec_contract_safety"]["ctVal_zec_per_contract"] = 0.1
        self.assertTrue(any("unresolved CTVAL" in item for item in validate_snapshot(snapshot)))

    def test_shadow_must_not_modify_baseline(self):
        snapshot = valid_snapshot()
        snapshot["zec_shadow_signal"]["baseline_unchanged"] = False
        self.assertTrue(any("BASELINE is unchanged" in item for item in validate_snapshot(snapshot)))

    def test_confidence_must_not_claim_calibration(self):
        snapshot = valid_snapshot()
        snapshot["zec_early_signal"]["confidence"]["calibrated"] = True
        self.assertTrue(any("calibrated" in item for item in validate_snapshot(snapshot)))


if __name__ == "__main__":
    unittest.main()
