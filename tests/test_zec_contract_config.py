import unittest
from unittest.mock import patch

from zec_contract_config import (
    build_contract_safety, CTVAL_RECONCILED, INSTRUMENT_ID, MAX_RISK_USDC,
    zec_for_contracts, usd_notional_for_contracts, usd_pnl_for_contracts,
    trading_fee_usd_for_contracts, funding_cashflow_usd_for_contracts,
    initial_margin_usd_for_contracts, maintenance_margin_usd_for_contracts,
    round_price_to_tick,
)


class ContractConfigTests(unittest.TestCase):
    def instrument(self, **changes):
        row = {"instId": INSTRUMENT_ID, "state": "live", "ctVal": "0.01",
               "ctMult": "1", "lotSz": "1", "minSz": "1",
               "tickSz": "0.01", "settleCcy": "USD", "ctValCcy": "ZEC", "ctType": "linear"}
        row.update(changes)
        return row

    def test_official_and_fresh_api_values_match_but_all_safety_gates_stay_closed(self):
        result = build_contract_safety(self.instrument(), verified=True)
        self.assertEqual(result["ctVal_status"], "CTVAL_VERIFICATO_SIZING_BLOCCATO")
        self.assertTrue(result["ctVal_ui_api_match"])
        self.assertTrue(result["ctVal_source_verified"])
        self.assertEqual(result["ctVal_api_raw"], 0.01)
        self.assertEqual(result["ctValCcy_api"], "ZEC")
        self.assertEqual(result["ctType_api"], "linear")
        self.assertEqual(result["ctMult_api"], 1)
        self.assertEqual(result["ctVal_zec_per_contract"], 0.01)
        self.assertEqual(result["ctVal_account_api_observed_zec"], 0.01)
        self.assertEqual(result["ctVal_okx_confirmed_zec"], 0.01)
        self.assertEqual(result["ctVal_official_spec_zec"], 0.01)
        self.assertTrue(CTVAL_RECONCILED is False)
        for field in ("ctVal_reconciled", "sizing_allowed", "theoretical_sizing_permitted",
                      "position_sizing_enabled", "orders_enabled", "margin_or_exposure_calculated"):
            self.assertFalse(result[field], field)
        self.assertEqual(result["max_risk_usdc"], MAX_RISK_USDC)

    def test_public_api_comparison_must_agree_with_official_contract(self):
        same = self.instrument()
        result = build_contract_safety(self.instrument(), verified=True, comparison_instrument=same)
        self.assertTrue(result["regional_api_values_match"])
        self.assertEqual(result["ctVal_status"], "CTVAL_VERIFICATO_SIZING_BLOCCATO")

        stale_spec = self.instrument(ctVal="0.1")
        result = build_contract_safety(self.instrument(), verified=True, comparison_instrument=stale_spec)
        self.assertFalse(result["ctVal_source_verified"])
        self.assertEqual(result["ctVal_status"], "FONTI_CTVAL_DISCORDANTI")
        self.assertIsNone(result["ctVal_zec_per_contract"])
        self.assertFalse(result["sizing_allowed"])

    def test_changed_or_stale_contract_metadata_never_unlocks_safety(self):
        result = build_contract_safety(self.instrument(ctVal="0.1"), verified=True)
        self.assertEqual(result["ctVal_status"], "METADATI_API_DISCORDANTI")
        self.assertFalse(result["ctVal_source_verified"])
        self.assertFalse(result["sizing_allowed"])

        result = build_contract_safety(self.instrument(), verified=False)
        self.assertEqual(result["ctVal_status"], "METADATI_API_NON_VERIFICATI")
        self.assertIsNone(result["ctVal_zec_per_contract"])
        self.assertIsNone(result["api_face_value_zec_diagnostic_only"])
        self.assertFalse(result["orders_enabled"])

    def test_unknown_ctval_currency_blocks_contract_match(self):
        for currency in (None, "USD"):
            result = build_contract_safety(self.instrument(ctValCcy=currency), verified=True)
            self.assertFalse(result["ctVal_ui_api_match"])
            self.assertFalse(result["sizing_allowed"])

    def test_one_contract_converts_to_001_zec_and_usd_notional(self):
        row = self.instrument()
        self.assertEqual(zec_for_contracts(1, row, verified=True), 0.01)
        self.assertEqual(usd_notional_for_contracts(1, 100, row, verified=True), 1.0)
        self.assertEqual(usd_notional_for_contracts(1, 862.61, row, verified=True), 8.6261)

    def test_multiplier_is_applied_to_contract_face_value(self):
        row = self.instrument(ctMult="2")
        with patch("zec_contract_config.CT_MULT", 2.0):
            self.assertEqual(zec_for_contracts(1, row, verified=True), 0.02)
            self.assertEqual(usd_notional_for_contracts(1, 100, row, verified=True), 2.0)

    def test_long_short_gross_pnl_uses_usd_margined_okx_formula(self):
        row = self.instrument()
        self.assertAlmostEqual(usd_pnl_for_contracts(3, 100, 110, "LONG", row, verified=True), 0.3)
        self.assertAlmostEqual(usd_pnl_for_contracts(3, 100, 110, "SHORT", row, verified=True), -0.3)
        self.assertAlmostEqual(usd_pnl_for_contracts(1, 862.61, 850, "LONG", row, verified=True), -0.1261)
        self.assertAlmostEqual(usd_pnl_for_contracts(1, 862.61, 850, "SHORT", row, verified=True), 0.1261)

    def test_fee_formula_uses_input_tier_rate_without_baking_in_account_tier(self):
        row = self.instrument()
        # OKX publishes LV1 maker/taker examples; the account fee tier is an input, not assumed.
        self.assertAlmostEqual(trading_fee_usd_for_contracts(1, 100, 0.0002, row, verified=True), 0.0002)
        self.assertAlmostEqual(trading_fee_usd_for_contracts(1, 100, 0.0005, row, verified=True), 0.0005)
        self.assertIsNone(trading_fee_usd_for_contracts(1, 100, -0.0005, row, verified=True))

    def test_funding_cashflow_sign_and_usd_value(self):
        row = self.instrument()
        # Positive rate: long pays and short receives. Negative rate reverses the transfer.
        self.assertAlmostEqual(funding_cashflow_usd_for_contracts(1, 100, 0.001, "LONG", row, verified=True), -0.001)
        self.assertAlmostEqual(funding_cashflow_usd_for_contracts(1, 100, 0.001, "SHORT", row, verified=True), 0.001)
        self.assertAlmostEqual(funding_cashflow_usd_for_contracts(1, 100, -0.001, "LONG", row, verified=True), 0.001)

    def test_margin_formulas_need_explicit_leverage_and_maintenance_tier(self):
        row = self.instrument()
        self.assertAlmostEqual(initial_margin_usd_for_contracts(1, 100, 10, row, verified=True), 0.1)
        self.assertAlmostEqual(maintenance_margin_usd_for_contracts(1, 100, 0.005, row, verified=True), 0.005)
        self.assertIsNone(initial_margin_usd_for_contracts(1, 100, 0, row, verified=True))

    def test_tick_precision_and_contract_lot_validation(self):
        row = self.instrument()
        self.assertEqual(round_price_to_tick(100.006, row), 100.01)
        self.assertEqual(round_price_to_tick(100.004, row), 100.0)
        self.assertIsNone(zec_for_contracts(0.5, row, verified=True))
        self.assertEqual(zec_for_contracts(2, row, verified=True), 0.02)

    def test_invalid_inputs_and_instrument_are_rejected(self):
        row = self.instrument()
        self.assertIsNone(zec_for_contracts(1, self.instrument(instId="ZEC-USDT-SWAP"), verified=True))
        self.assertIsNone(usd_notional_for_contracts(1, -1, row, verified=True))
        self.assertIsNone(usd_pnl_for_contracts(1, 100, 110, "BUY", row, verified=True))
        self.assertIsNone(round_price_to_tick(100, self.instrument(tickSz="0")))


if __name__ == "__main__":
    unittest.main()
