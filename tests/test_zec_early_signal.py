import unittest
from datetime import datetime, timezone

from zec_early_signal import (
    INSTRUMENT_ID, _anti_chase_veto, _directional_candidate_scores, evaluate_signal,
)


CAPTURED = "2026-01-01T12:10:00Z"
NOW = datetime(2026, 1, 1, 12, 10, tzinfo=timezone.utc)


def candles(bar, base_price, interval_minutes, count=8, drift=1.0):
    interval_ms = interval_minutes * 60_000
    end_ms = int(NOW.timestamp() * 1000)
    latest_start = end_ms - interval_ms
    rows = []
    for index in range(count):
        price = base_price + index * drift
        start = latest_start - (count - index - 1) * interval_ms
        rows.append({
            "ts_ms": start,
            "completed_at": datetime.fromtimestamp((start + interval_ms) / 1000, timezone.utc).isoformat().replace("+00:00", "Z"),
            "open": price - 0.2,
            "high": price + 0.3,
            "low": price - 0.5,
            "close": price,
            "volume": 100,
        })
    return rows


def market_record(inst_id, fields=None):
    return {
        "http_status": 200,
        "api_code": "0",
        "exchange_age_seconds": 1,
        "first": {"instId": inst_id, **(fields or {})},
    }


def valid_snapshot():
    tail = {}
    for instrument, price in (("ZEC-USD_UM_XPERP", 100), ("ZEC-USDC", 100), ("BTC-USDC", 1000)):
        tail[instrument] = {
            "1m": {"tail": candles("1m", price, 1)},
            "5m": {"tail": candles("5m", price, 5)},
        }
    funding_time_ms = int(NOW.timestamp() * 1000)
    return {
        "schema_version": 3,
        "source_captured_at": CAPTURED,
        "source_status": "ok",
        "render_health_http": 200,
        "render_candles_http": 200,
        "render_health": {"status": "ok", "websocket_connected": True},
        "read_errors": {},
        "market": {
            "zec_xperp_ticker": market_record(INSTRUMENT_ID),
            "zec_xperp_instrument": market_record(INSTRUMENT_ID, {"ctVal": "0.01", "ctValCcy": "ZEC", "ctType": "linear", "ctMult": "1", "lotSz": "1", "minSz": "1", "tickSz": "0.01", "settleCcy": "USD", "state": "live"}),
            "zec_xperp_funding": market_record(INSTRUMENT_ID, {
                "prevFundingTime": str(funding_time_ms - 4 * 60 * 60 * 1000),
                "fundingTime": str(funding_time_ms),
                "nextFundingTime": str(funding_time_ms + 4 * 60 * 60 * 1000),
                "fundingRate": "0.0001",
                "nextFundingRate": "",
            }),
            "zec_spot_ticker": market_record("ZEC-USDC"),
            "btc_spot_ticker": market_record("BTC-USDC"),
        },
        "candles_1m_5m": tail,
        "microstructure": {
            "websocket": {
                "connected": True, "book_valid": True,
                "message_age_seconds": 1, "trade_age_seconds": 1, "book_age_seconds": 1,
                "book_history": [
                    {"ts_ms": int(NOW.timestamp() * 1000) - 5_000, "imbalance_top10": 0.2},
                    {"ts_ms": int(NOW.timestamp() * 1000), "imbalance_top10": 0.8},
                ],
            },
            "cvd": {"ready_1m": True, "ready_5m": True, "1m": 10, "5m": 20},
            "book_imbalance": {"imbalance_top10": 0.8},
            "open_interest_history": [
                {"ts_ms": int(NOW.timestamp() * 1000) - 120_000, "oi_contracts": "100", "price_usd": "100", "inst_id": INSTRUMENT_ID},
                {"ts_ms": int(NOW.timestamp() * 1000), "oi_contracts": "101", "price_usd": "101", "inst_id": INSTRUMENT_ID},
            ],
        },
    }


class ZecEarlySignalTests(unittest.TestCase):
    def test_breakout_warning_is_separate_and_never_enables_orders(self):
        snapshot = valid_snapshot()
        rows = snapshot["candles_1m_5m"]["ZEC-USD_UM_XPERP"]["5m"]["tail"]
        rows[-1]["volume"] = 500
        result = evaluate_signal(snapshot, now=NOW)
        warning = result["breakout_early_warning"]
        self.assertEqual(warning["candidate_direction"], "LONG")
        self.assertIn("BREAKOUT", warning["status"])
        self.assertFalse(warning["orders_enabled"])
        self.assertFalse(result["execution"]["orders_enabled"])

    def test_breakout_warning_never_confirms_without_volume(self):
        snapshot = valid_snapshot()
        rows = snapshot["candles_1m_5m"]["ZEC-USD_UM_XPERP"]["5m"]["tail"]
        rows[-1]["volume"] = 100
        result = evaluate_signal(snapshot, now=NOW)
        warning = result["breakout_early_warning"]
        self.assertEqual(warning["status"], "ROTTURA RIALZISTA NON CONFERMATA")
        self.assertFalse(warning["orders_enabled"])

    def test_anti_chase_veto_is_symmetric_for_long_and_short(self):
        self.assertIsNotNone(_anti_chase_veto("LONG", {"status": "PRE-ALLERTA LONG"}))
        self.assertIsNotNone(_anti_chase_veto("SHORT", {"status": "PRE-ALLERTA SHORT"}))
        self.assertIsNotNone(_anti_chase_veto("LONG", {"status": "BREAKOUT GIA' ESTESO — ATTENDI RETEST"}))
        self.assertIsNotNone(_anti_chase_veto("SHORT", {"status": "BREAKDOWN GIA' ESTESO — ATTENDI RETEST"}))
        self.assertIsNone(_anti_chase_veto("LONG", {"status": "BREAKOUT RIALZISTA"}))
        self.assertIsNone(_anti_chase_veto("SHORT", {"status": "BREAKDOWN RIBASSISTA"}))

    def test_directional_candidates_keep_long_and_short_evidence_separate(self):
        scores = _directional_candidate_scores({
            "price": {"score": 0.8, "weight": 0.5},
            "relative_strength": {"score": -0.4, "weight": 0.5},
        })
        self.assertEqual(scores["long"]["score"], 40.0)
        self.assertEqual(scores["short"]["score"], 20.0)
        self.assertEqual(scores["unit"], "heuristic evidence points, not probability")

    def test_unconfirmed_long_breakout_cannot_become_confirmed_entry(self):
        result = evaluate_signal(valid_snapshot(), now=NOW)
        self.assertEqual(result["anti_chase"]["blocked"], True)
        self.assertIn("resistenza/rottura non ancora confermata", result["anti_chase"]["reason"])
        self.assertNotEqual(result["decision"], "LONG")

    def test_anti_chase_veto_is_symmetric_for_long_and_short(self):
        self.assertIsNotNone(_anti_chase_veto("LONG", {"status": "PRE-ALLERTA LONG"}))
        self.assertIsNotNone(_anti_chase_veto("SHORT", {"status": "PRE-ALLERTA SHORT"}))
        self.assertIsNotNone(_anti_chase_veto("LONG", {"status": "BREAKOUT GIA' ESTESO — ATTENDI RETEST"}))
        self.assertIsNotNone(_anti_chase_veto("SHORT", {"status": "BREAKDOWN GIA' ESTESO — ATTENDI RETEST"}))
        self.assertIsNone(_anti_chase_veto("LONG", {"status": "BREAKOUT RIALZISTA"}))
        self.assertIsNone(_anti_chase_veto("SHORT", {"status": "BREAKDOWN RIBASSISTA"}))

    def test_directional_candidates_keep_long_and_short_evidence_separate(self):
        scores = _directional_candidate_scores({
            "price": {"score": 0.8, "weight": 0.5},
            "relative_strength": {"score": -0.4, "weight": 0.5},
        })
        self.assertEqual(scores["long"]["score"], 40.0)
        self.assertEqual(scores["short"]["score"], 20.0)
        self.assertEqual(scores["unit"], "heuristic evidence points, not probability")

    def test_unconfirmed_long_breakout_cannot_become_confirmed_entry(self):
        result = evaluate_signal(valid_snapshot(), now=NOW)
        self.assertTrue(result["anti_chase"]["blocked"])
        self.assertIn("resistenza/rottura non ancora confermata", result["anti_chase"]["reason"])
        self.assertNotEqual(result["decision"], "LONG")

    def test_missing_snapshot_waits_and_has_no_directional_decision(self):
        result = evaluate_signal({}, now=NOW)
        self.assertEqual(result["status"], "ATTENDERE")
        self.assertEqual(result["decision"], "ATTENDERE")
        self.assertFalse(result["execution"]["orders_enabled"])

    def test_exact_xperp_contract_is_required(self):
        snapshot = valid_snapshot()
        snapshot["market"]["zec_xperp_ticker"]["first"]["instId"] = "ZEC-USDT-SWAP"
        result = evaluate_signal(snapshot, now=NOW)
        self.assertEqual(result["status"], "ATTENDERE")
        self.assertIn(INSTRUMENT_ID, " ".join(result["data_quality"]["blockers"]))

    def test_flow_and_book_are_one_correlated_score_group(self):
        result = evaluate_signal(valid_snapshot(), now=NOW)
        self.assertEqual(result["scores"]["groups"]["order_flow"]["weight"], 0.35)
        self.assertTrue(any("dinamica imbalance" in item for item in result["scores"]["groups"]["order_flow"]["factors"]))
        self.assertNotIn("cvd", result["scores"]["groups"])
        self.assertNotIn("book", result["scores"]["groups"])

    def test_valid_strong_setup_is_simulation_only_and_never_sizes(self):
        snapshot = valid_snapshot()
        snapshot["candles_1m_5m"]["ZEC-USD_UM_XPERP"]["5m"]["tail"][-1]["volume"] = 200
        result = evaluate_signal(snapshot, now=NOW)
        self.assertEqual(result["status"], "TRIGGER CONFERMATO")
        self.assertEqual(result["decision"], "LONG")
        self.assertFalse(result["anti_chase"]["blocked"])
        self.assertGreater(result["scores"]["directional_candidates"]["long"]["score"], 0)
        self.assertEqual(result["scores"]["directional_candidates"]["short"]["score"], 0)
        self.assertEqual(result["instrument"]["inst_id"], INSTRUMENT_ID)
        self.assertEqual(result["contract_safety"]["ctVal_status"], "DISCREPANZA_CTVAL_NON_RISOLTA")
        self.assertTrue(result["contract_safety"]["ctVal_ui_api_match"])
        self.assertIsNone(result["contract_safety"]["ctVal_zec_per_contract"])
        self.assertEqual(result["contract_safety"]["ctVal_official_spec_zec"], 0.1)
        self.assertEqual(result["contract_safety"]["account_api_ctVal_observed_zec"], 0.01)
        self.assertEqual(result["contract_safety"]["ctMult_api"], 1)
        self.assertFalse(result["contract_safety"]["sizing_allowed"])
        self.assertFalse(result["contract_safety"]["theoretical_sizing_permitted"])
        self.assertEqual(result["contract_safety"]["max_risk_usdc"], 15)
        self.assertFalse(result["contract_safety"]["position_sizing_enabled"])
        self.assertFalse(result["execution"]["orders_enabled"])
        self.assertEqual(result["exploratory_direction"], "LONG")
        self.assertIsNone(result["execution"]["position_quantity"])
        self.assertIsNone(result["execution"]["margin"])
        self.assertIsNone(result["execution"]["pnl"])

    def test_api_contract_mismatch_blocks_theoretical_sizing(self):
        snapshot = valid_snapshot()
        snapshot["market"]["zec_xperp_instrument"]["first"]["ctVal"] = "0.1"
        result = evaluate_signal(snapshot, now=NOW)
        self.assertEqual(result["contract_safety"]["ctVal_status"], "METADATI_API_DISCORDANTI")
        self.assertFalse(result["contract_safety"]["sizing_allowed"])
        self.assertEqual(result["contract_safety"]["max_risk_usdc"], 15)

    def test_funding_interval_mismatch_is_reported_without_affecting_safety(self):
        snapshot = valid_snapshot()
        funding = snapshot["market"]["zec_xperp_funding"]["first"]
        funding["prevFundingTime"] = str(int(funding["fundingTime"]) - 8 * 60 * 60 * 1000)
        funding["nextFundingTime"] = str(int(funding["fundingTime"]) + 8 * 60 * 60 * 1000)
        result = evaluate_signal(snapshot, now=NOW)
        diagnostic = result["contract_safety"]["funding_interval"]
        self.assertEqual(diagnostic["status"], "API_TIMESTAMP_NON_CONFORMI_SPECIFICA_4H")
        self.assertEqual(diagnostic["observed_intervals"]["previous_to_current_hours"], 8.0)
        self.assertEqual(diagnostic["live_next_interval_hours"], 8.0)
        self.assertFalse(result["contract_safety"]["sizing_allowed"])
        self.assertFalse(result["execution"]["orders_enabled"])
        self.assertTrue(any("Funding:" in item for item in result["data_quality"]["limitations"]))

    def test_stale_snapshot_waits(self):
        snapshot = valid_snapshot()
        snapshot["source_captured_at"] = "2026-01-01T12:00:00Z"
        result = evaluate_signal(snapshot, now=NOW)
        self.assertEqual(result["status"], "ATTENDERE")
        self.assertEqual(result["decision"], "ATTENDERE")

    def test_unresolved_oi_reduces_confidence_but_is_optional(self):
        snapshot = valid_snapshot()
        snapshot["microstructure"]["open_interest_history"] = []
        result = evaluate_signal(snapshot, now=NOW)
        self.assertNotIn("positioning", result["scores"]["groups"])
        self.assertTrue(result["data_quality"]["limitations"])
        self.assertLessEqual(result["confidence"]["score"], 65)

    def test_unclosed_candle_blocks_signal(self):
        snapshot = valid_snapshot()
        snapshot["candles_1m_5m"]["ZEC-USD_UM_XPERP"]["1m"]["tail"][-1]["completed_at"] = "2026-01-01T12:11:00Z"
        result = evaluate_signal(snapshot, now=NOW)
        self.assertEqual(result["status"], "ATTENDERE")
        self.assertEqual(result["decision"], "ATTENDERE")


if __name__ == "__main__":
    unittest.main()
