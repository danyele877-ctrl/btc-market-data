import unittest

from zec_contract_config import build_contract_safety, INSTRUMENT_ID, MAX_RISK_USDC


class ContractConfigTests(unittest.TestCase):
    def instrument(self, **changes):
        row = {"instId": INSTRUMENT_ID, "state": "live", "ctVal": "0.01",
               "ctMult": "1", "lotSz": "1", "minSz": "1",
               "tickSz": "0.01", "settleCcy": "USD", "ctValCcy": "ZEC", "ctType": "linear"}
        row.update(changes)
        return row

    def test_matching_ui_and_api_metadata_still_blocks_sizing_until_official_reconciliation(self):
        result = build_contract_safety(self.instrument(), verified=True)
        self.assertEqual(result["ctVal_status"], "SPECIFICA_UFFICIALE_0_1_CONFERMATA_API_ACCOUNT_DISCORDANTE")
        self.assertTrue(result["ctVal_ui_api_match"])
        self.assertEqual(result["ctVal_api_raw"], 0.01)
        self.assertEqual(result["ctValCcy_api"], "ZEC")
        self.assertEqual(result["ctType_api"], "linear")
        self.assertEqual(result["ctVal_official_spec_zec"], 0.1)
        self.assertTrue(result["official_support_confirmation_received"])
        self.assertEqual(result["official_support_confirmed_contract_size_zec"], 0.1)
        self.assertFalse(result["sizing_allowed"])
        self.assertFalse(result["theoretical_sizing_permitted"])
        self.assertFalse(result["position_sizing_enabled"])
        self.assertFalse(result["orders_enabled"])
        self.assertEqual(result["max_risk_usdc"], 20)

    def test_api_face_value_is_diagnostic_only_and_global_comparison_never_unlocks_sizing(self):
        global_row = self.instrument(ctVal="0.1")
        result = build_contract_safety(
            self.instrument(), verified=True, comparison_instrument=global_row
        )
        self.assertEqual(result["api_face_value_zec_diagnostic_only"], 0.01)
        self.assertEqual(result["global_api_face_value_zec_diagnostic_only"], 0.1)
        self.assertFalse(result["regional_api_values_match"])
        self.assertEqual(result["ctVal_status"], "SPECIFICA_UFFICIALE_0_1_CONFERMATA_API_ACCOUNT_DISCORDANTE")
        self.assertFalse(result["sizing_allowed"])
        self.assertFalse(result["theoretical_sizing_permitted"])
        self.assertFalse(result["orders_enabled"])

    def test_missing_or_changed_contract_metadata_blocks_sizing(self):
        result = build_contract_safety(self.instrument(ctVal="0.1"), verified=True)
        self.assertEqual(result["ctVal_status"], "NON VERIFICATO")
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
        self.assertEqual(result["ctVal_status"], "NON VERIFICATO")
        self.assertFalse(result["sizing_allowed"])


if __name__ == "__main__":
    unittest.main()
