import unittest
from datetime import datetime, timedelta, timezone

from zec_sec_news import BTC_ID, ZEC_STREAM, market_reactions, material_change, recent_filings


class StrategyNewsTests(unittest.TestCase):
    def test_routine_filing_is_not_material_and_policy_change_is(self):
        self.assertIsNone(material_change("<p>Strategy bought 457 bitcoin during the week.</p>"))
        self.assertEqual(
            material_change("<p>Its board approved an increase in the aggregate purchase price "
                            "authorized under its digital credit securities repurchase program.</p>"),
            "Modifica al programma di riacquisto",
        )

    def test_official_filing_links_and_freshness(self):
        now = datetime(2026, 11, 23, 13, 5, tzinfo=timezone.utc)
        recent = dict(form=["8-K", "8-K"], acceptanceDateTime=[now.isoformat(), (now-timedelta(hours=1)).isoformat()],
                      accessionNumber=["0001193125-26-470961", "0001193125-26-470960"],
                      primaryDocument=["strategy.htm", "strategy.htm"], items=["7.01,8.01", "8.01"])
        filings = list(recent_filings({"filings": {"recent": recent}}, now))
        self.assertEqual(len(filings), 1)
        self.assertTrue(filings[0][2].endswith("/000119312526470961/strategy.htm"))

    def test_reaction_uses_only_full_closed_aligned_minutes(self):
        start = datetime(2026, 11, 23, 13, tzinfo=timezone.utc)
        rows = [{"completed_at": (start+timedelta(minutes=i)).isoformat(), "open": 100,
                 "high": 110, "low": 90, "close": 100+i} for i in range(1, 7)]
        instruments = {key: {"1m": rows} for key in (BTC_ID, ZEC_STREAM)}
        accepted = start+timedelta(seconds=14)
        for elapsed, expected in [(1, ()), (2, (1,)), (6, (1, 5))]:
            reactions = market_reactions(instruments, accepted, start+timedelta(minutes=elapsed))
            self.assertEqual(tuple(reactions), expected)
            if reactions:
                self.assertAlmostEqual(reactions[1][0], 2)
        instruments[ZEC_STREAM]["1m"] = rows[:2] + rows[3:]
        self.assertNotIn(5, market_reactions(instruments, accepted, start+timedelta(minutes=6)))
