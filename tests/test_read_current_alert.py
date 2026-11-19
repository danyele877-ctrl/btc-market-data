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
