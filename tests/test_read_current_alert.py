import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from read_current_alert import INSTRUMENTS, read_current_alert, validated_side


class CurrentAlertReaderTests(unittest.TestCase):
    def test_validated_decision_requires_freshness_trigger_and_invalidation(self):
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=30)
        document = {
            "schema_version": 1, "captured_at": now.isoformat(), "generated_at": now.isoformat(),
            "feed": {"status": "ok", "fresh_at_publication": True, "expires_at": expires.isoformat()},
            "quality": {"engine_ready": True, "required_prices_available": True},
            "decision": {"side": "LONG", "reason": "confirmed_trigger", "invalidation_price": 10},
            "prices": {instrument: {"last": 11, "mark": 11} for instrument in INSTRUMENTS},
        }
        self.assertEqual(validated_side(document, now), "LONG")
        self.assertEqual(validated_side(document, expires), "NO_SIGNAL")
        document["prices"][INSTRUMENTS[-1]]["mark"] = 10
        self.assertEqual(validated_side(document, now), "NO_SIGNAL")
        document["prices"][INSTRUMENTS[-1]]["mark"] = 11
        document["decision"]["reason"] = "no_confirmed_trigger"
        self.assertEqual(validated_side(document, now), "NO_SIGNAL")


    def test_primary_fresh_no_signal_is_authoritative(self):
        now = datetime.now(timezone.utc)
        document = {
            "schema_version": 1, "captured_at": now.isoformat(),
            "generated_at": now.isoformat(),
            "feed": {"status": "ok", "fresh_at_publication": True,
                     "expires_at": (now + timedelta(seconds=60)).isoformat()},
            "quality": {"engine_ready": True, "required_prices_available": True},
            "decision": {"side": "NO_SIGNAL", "reason": "no_confirmed_trigger"},
            "prices": {instrument: {"last": 11} for instrument in INSTRUMENTS},
        }
        with patch("read_current_alert.get_json", return_value=document) as fetch:
            result = read_current_alert()
        self.assertEqual(result["source"], "NORTHFLANK")
        self.assertEqual(result["decision"], "NO_SIGNAL")
        self.assertEqual(fetch.call_count, 1)

    def test_unavailable_primary_uses_fresh_github_mirror(self):
        import base64
        import json
        now = datetime.now(timezone.utc)
        document = {
            "schema_version": 1, "captured_at": now.isoformat(),
            "generated_at": now.isoformat(),
            "feed": {"status": "ok", "fresh_at_publication": True,
                     "expires_at": (now + timedelta(seconds=60)).isoformat()},
            "quality": {"engine_ready": True, "required_prices_available": True},
            "decision": {"side": "NO_SIGNAL", "reason": "no_confirmed_trigger"},
            "prices": {instrument: {"last": 11} for instrument in INSTRUMENTS},
        }
        contents = {"encoding": "base64", "sha": "test-sha",
                    "content": base64.encodebytes(json.dumps(document).encode()).decode()}
        with patch("read_current_alert.get_json", side_effect=[OSError(), contents]) as fetch:
            result = read_current_alert()
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(result["source"], "GITHUB")
        self.assertEqual(result["blob_sha"], "test-sha")
        self.assertEqual(result["decision"], "NO_SIGNAL")

    def test_fallback_excludes_stale_and_future_quotes_and_never_emits_signal(self):
        now = datetime.now(timezone.utc)
        observations = dict(zip(INSTRUMENTS, (
            (11, now), (12, now - timedelta(seconds=181)), (13, now + timedelta(seconds=5)),
        )))
        with patch("read_current_alert.get_json", side_effect=OSError), patch(
            "read_current_alert.read_ticker", side_effect=observations.__getitem__,
        ):
            result = read_current_alert()
        self.assertEqual(result["decision"], "NO_SIGNAL")
        self.assertEqual(list(result["prices"]), [INSTRUMENTS[0]])
