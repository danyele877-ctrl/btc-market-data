import unittest

from zec_contract_config import build_contract_safety, INSTRUMENT_ID, MAX_RISK_USDC


class ContractConfigTests(unittest.TestCase):
    def instrument(self, **changes):
        row = {"instId": INSTRUMENT_ID, "state": "live", "ctVal": "0.01",
               "ctMult": "1", "lotSz": "1", "minSz": "1",
               "tickSz": "0.01", "settleCcy": "USD"}
        row.update(changes)
        return row

    def test_matching_fresh_eea_metadata_allows_theoretical_sizing_only(self):
        result = build_contract_safety(self.instrument(), verified=True)
        self.assertEqual(result["ctVal_status"], "RISOLTO_UI_API_CONCORDI")
        self.assertTrue(result["sizing_allowed"])
        self.assertTrue(result["theoretical_sizing_permitted"])
        self.assertFalse(result["position_sizing_enabled"])
        self.assertFalse(result["orders_enabled"])
        self.assertEqual(result["max_risk_usdc"], 20)

    def test_missing_or_changed_contract_metadata_blocks_sizing(self):
        result = build_contract_safety(self.instrument(ctVal="0.1"), verified=True)
        self.assertEqual(result["ctVal_status"], "NON VERIFICATO")
        self.assertFalse(result["sizing_allowed"])
        self.assertEqual(result["max_risk_usdc"], MAX_RISK_USDC)

    def test_stale_metadata_never_allows_sizing(self):
        result = build_contract_safety(self.instrument(), verified=False)
        self.assertEqual(result["ctVal_status"], "NON VERIFICATO")
        self.assertFalse(result["sizing_allowed"])


if __name__ == "__main__":
    unittest.main()
