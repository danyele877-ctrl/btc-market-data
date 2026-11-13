import unittest

import quality_check


class OkxEeaPayloadTests(unittest.TestCase):
    def test_xperp_order_book_accepts_real_okx_shape_without_inst_id(self):
        payload = {
            "code": "0",
            "data": [{
                "asks": [["936.0", "9", "0", "1"]],
                "bids": [["935.9", "12", "0", "1"]],
                "ts": "1794606457152",
                "seqId": 123,
            }],
        }
        self.assertTrue(quality_check.valid_source_payload("okx_eea_zec_xperp_book", payload))

    def test_xperp_order_book_rejects_empty_sides(self):
        payload = {
            "code": "0",
            "data": [{
                "asks": [],
                "bids": [["935.9", "12", "0", "1"]],
                "ts": "1794606457152",
            }],
        }
        self.assertFalse(quality_check.valid_source_payload("okx_eea_zec_xperp_book", payload))

    def test_xperp_mark_requires_exact_instrument(self):
        good = {"code": "0", "data": [{"instId": "ZEC-USD_UM_XPERP-310530", "markPx": "935.8"}]}
        wrong = {"code": "0", "data": [{"instId": "ZEC-USDT-SWAP", "markPx": "935.8"}]}
        self.assertTrue(quality_check.valid_source_payload("okx_eea_zec_xperp_mark", good))
        self.assertFalse(quality_check.valid_source_payload("okx_eea_zec_xperp_mark", wrong))


if __name__ == "__main__":
    unittest.main()
