import unittest
from unittest.mock import patch

from zec_contract_config import (
    build_contract_safety, INSTRUMENT_ID, MAX_RISK_USDC,
    zec_for_contracts, usd_notional_for_contracts, usd_pnl_for_contracts,
)


class ContractConfigTests(unittest.TestCase):
    def instrument(self, **changes):
        row = {"instId": INSTRUMENT_ID, "state": "live", "ctVal": "0.01",
               "ctMult": "1", "lotSz": "1", "minSz": "1",
               "tickSz": "0.01", "settleCcy": "USD", "ctValCcy": "ZEC", "ctType": "linear"}
        row.update(changes)
        return row

    def test_public_spec_and_account_api_values_remain_separate_and_unresolved(self):
        result = build_contract_safety(self.instrument(), verified=True)
        self.assertEqual(result["ctVal_status"], "DISCREPANZA_CTVAL_NON_RISOLTA")
        self.assertTrue(result["ctVal_ui_api_match"])
        self.assertEqual(result["ctVal_api_raw"], 0.01)
        self.assertEqual(result["ctValCcy_api"], "ZEC")
        self.assertEqual(result["ctType_api"], "linear")
        self.assertEqual(result["ctMult_api"], 1)
        self.assertIsNone(result["ctVal_zec_per_contract"])
        self.assertEqual(result["ctVal_account_api_observed_zec"], 0.01)
        self.assertEqual(result["ctVal_okx_confirmed_zec"], 0.1)
        self.assertEqual(result["ctVal_official_spec_zec"], 0.1)
        self.assertEqual(result["ctMult_account_api_observed"], 1)
        self.assertTrue(result["official_support_confirmation_received"])
        self.assertEqual(result["official_support_confirmed_contract_size_zec"], 0.1)
        self.assertFalse(result["ctVal_reconciled"])
        self.assertFalse(result["sizing_allowed"])
        self.assertFalse(result["theoretical_sizing_permitted"])
        self.assertFalse(result["position_sizing_enabled"])
        self.assertFalse(result["orders_enabled"])
        self.assertEqual(result["max_risk_usdc"], 15)

    def test_api_face_value_is_diagnostic_only_and_global_comparison_never_unlocks_sizing(self):
        global_row = self.instrument(ctVal="0.1")
        result = build_contract_safety(
            self.instrument(), verified=True, comparison_instrument=global_row
        )
        self.assertEqual(result["api_face_value_zec_diagnostic_only"], 0.01)
        self.assertEqual(result["global_api_face_value_zec_diagnostic_only"], 0.1)
        self.assertFalse(result["regional_api_values_match"])
        self.assertEqual(result["ctVal_status"], "DISCREPANZA_CTVAL_NON_RISOLTA")
        self.assertFalse(result["sizing_allowed"])
        self.assertFalse(result["theoretical_sizing_permitted"])
        self.assertFalse(result["orders_enabled"])

    def test_missing_or_changed_contract_metadata_blocks_sizing(self):
        result = build_contract_safety(self.instrument(ctVal="0.1"), verified=True)
        self.assertEqual(result["ctVal_status"], "METADATI_API_DISCORDANTI")
        self.assertFalse(result["ctVal_reconciled"])
        self.assertFalse(result["sizing_allowed"])
        self.assertEqual(result["max_risk_usdc"], MAX_RISK_USDC)

    def test_unknown_or_non_zec_ctval_currency_blocks_match(self):
        for currency in (None, "USD"):
            result = build_contract_safety(self.instrument(ctValCcy=currency), verified=True)
            self.assertFalse(result["ctVal_ui_api_match"])
            self.assertFalse(result["sizing_allowed"])
            self.assertEqual(result["ctVal_api_raw"], 0.01)

    def test_stale_metadata_never_allows_sizing(self):
        result = build_contract_safety(self.instrument(), verified=False)
        self.assertEqual(result["ctVal_status"], "METADATI_API_NON_VERIFICATI")
        self.assertIsNone(result["ctVal_zec_per_contract"])
        self.assertIsNone(result["api_face_value_zec_diagnostic_only"])
        self.assertFalse(result["sizing_allowed"])

    def test_contract_zec_usd_notional_and_pnl_conversions_use_api_value_and_multiplier(self):
        row = self.instrument()
        with patch("zec_contract_config.CTVAL_RECONCILED", True), patch(
            "zec_contract_config.CTVAL_ZEC_PER_CONTRACT", 0.01
        ):
            self.assertAlmostEqual(zec_for_contracts(3, row, verified=True), 0.03)
            self.assertAlmostEqual(usd_notional_for_contracts(1, 100, row, verified=True), 1.0)
            self.assertAlmostEqual(usd_notional_for_contracts(3, 100, row, verified=True), 3.0)
            self.assertAlmostEqual(usd_notional_for_contracts(1, 862.61, row, verified=True), 8.6261)
            self.assertAlmostEqual(usd_pnl_for_contracts(3, 100, 110, "LONG", row, verified=True), 0.3)
            self.assertAlmostEqual(usd_pnl_for_contracts(3, 100, 110, "SHORT", row, verified=True), -0.3)
            self.assertAlmostEqual(usd_pnl_for_contracts(1, 862.61, 850, "LONG", row, verified=True), -0.1261)
            self.assertIsNone(zec_for_contracts(1, self.instrument(instId="ZEC-USDT-SWAP"), verified=True))
            self.assertIsNone(zec_for_contracts(0.5, row, verified=True))
            self.assertIsNone(usd_notional_for_contracts(1, -1, row, verified=True))
            self.assertIsNone(usd_pnl_for_contracts(1, 100, 110, "BUY", row, verified=True))

    def test_unresolved_ctval_blocks_all_operational_conversions(self):
        row = self.instrument()
        self.assertIsNone(zec_for_contracts(1, row, verified=True))
        self.assertIsNone(usd_notional_for_contracts(1, 100, row, verified=True))
        self.assertIsNone(usd_pnl_for_contracts(1, 100, 110, "LONG", row, verified=True))


if __name__ == "__main__":
    unittest.main()
