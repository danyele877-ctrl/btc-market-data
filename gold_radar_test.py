import gzip
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from datetime import date, datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request,urlopen

from gold_history import INST
from gold_calendar import parse as parse_bls
from gold_calendar_unified import monthly_events
from gold_macro_probe import aligned_hour, xau_same_hour, macro_for_display
from gold_market_view import analyze
from gold_notifications import events
from gold_paper import advance, replay_legacy
from gold_paper_history import backfill
from gold_quotes import snapshot
from gold_strategies import evaluate
from gold_tracking import save
from gold_web import paper_page, serve


def setup(direction="LONG"):
    return {"model":"TECHNICAL","direction":direction,"cohort":"PAPER_TEST_CANDIDATE",
            "observed_at":3,"plan":{"entry_limit":100,"stop":95 if direction=="LONG" else 105,
            "tp1":105 if direction=="LONG" else 95,"tp2":110 if direction=="LONG" else 90,
            "tp3":115 if direction=="LONG" else 85},"step":60,"cursor":60,"expires_at":240,
            "holding_seconds":600,"state":"PENDING","bar":"1m"}


def raw(t, state="1"):
    return [str(t*1000),"100","102","99","101","2","2","202",state]


class GoldRadar(unittest.TestCase):
    def test_contiguous_then_retry_gap_and_record_stop(self):
        item = setup()
        advance(item, [[60,101,103,99,102,1],[180,103,104,94,98,1]], 240)
        self.assertEqual(("DATA_GAP",120), (item["state"],item["cursor"]))
        advance(item, [[120,102,106,100,103,1],[180,103,104,94,98,1]], 240)
        self.assertEqual(("STOP",{"tp1":180},False), (item["state"],item["targets_touched"],item["fill_verified"]))

    def test_ambiguous_short_stop_and_target(self):
        item=setup("SHORT")
        advance(item,[[60,99,101,98,99,2],[120,99,106,93,100,5]],180)
        self.assertEqual(item["state"],"AMBIGUOUS")
        self.assertEqual(item["ambiguity"],"target_and_stop")
        self.assertNotIn("exit_price",item)

    def test_expiry_does_not_require_future_bar(self):
        item=setup()
        advance(item,[[t,101,103,100,102,1] for t in (60,120,180)],240)
        self.assertEqual((item["state"],item["ended_at"]),("EXPIRED",240))
        self.assertNotIn("filled_at",item)

    def test_same_bar_entry_and_target_is_unsequenced(self):
        item=setup()
        advance(item,[[60,101,111,99,100,1]],120)
        self.assertEqual(item["unsequenced_entry_targets"],["tp1","tp2"])
        self.assertNotIn("targets_touched",item)

    def test_legacy_repair_keeps_frozen_fields(self):
        item=setup()
        item.update(state="DATA_GAP",cursor=120)
        original=item["plan"].copy(),item["expires_at"]
        self.assertFalse(replay_legacy(item,[[120,102,105,101,104,1]],180))
        self.assertTrue(replay_legacy(item,[[60,101,103,99,102,2],[120,102,116,101,114,2]],180))
        self.assertEqual(item["state"],"FINAL_TARGET_TOUCHED")
        self.assertEqual((item["plan"],item["expires_at"]),original)
        self.assertEqual(item["legacy_outcome"]["state"],"DATA_GAP")

    def test_history_pages_validate_instrument_and_closed_candles(self):
        def fetch(path,**params):
            self.assertEqual(INST,params["instId"])
            self.assertEqual("/market/history-candles",path)
            self.assertEqual("180000",params["after"])
            return [raw(120,"0"),raw(60),raw(60)]
        self.assertEqual([x[0] for x in backfill("1m",60,180,60,fetch)],[60])
        with self.assertRaises(ValueError):
            analyze([raw(61)],"1m",180)

    def test_macro_exact_half_hour_window(self):
        macro={"dxy":{"validated":True,"closed_observations":[(1800,100),(5400,99)]},
               "gold_futures_proxy":{"validated":True,"closed_observations":[(1800,200),(5400,204)]}}
        interval=aligned_hour(macro,9100)
        self.assertEqual((interval["start"],interval["end"]),(5400,9000))
        rows=[raw(t) for t in range(5400,9000,60)]
        self.assertAlmostEqual(xau_same_hour(rows,interval),.01)
        self.assertIsNone(xau_same_hour(rows[:-1],interval))
        self.assertIsNone(aligned_hour(macro,15000))

    def test_macro_display_keeps_historical_close_on_source_error(self):
        prior = {"dxy":{"identity_verified":True,"closed_observations":[[1800,104.05]],"hourly_change_pct":None}}
        failed = {"dxy":{"error":"unavailable"}}
        retained = macro_for_display(failed,prior)
        self.assertTrue(retained["dxy"]["cached"])
        self.assertEqual(retained["dxy"]["closed_observations"][-1][1],104.05)
        self.assertIsNone(aligned_hour(failed,5401))

    def test_bls_eastern_alias_next_month_and_html_fallback(self):
        now = datetime(2026,11,28,tzinfo=timezone.utc)
        wire = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Consumer Price Index\nDTSTART;TZID=US-Eastern:20261210T083000\nEND:VEVENT\nEND:VCALENDAR"
        self.assertEqual(parse_bls(wire,now)[0]["scheduled_at"],"2026-12-10T13:30:00+00:00")
        html = '<table><tr><td class="date-cell">Friday, December 04, 2026</td><td class="time-cell">08:30 AM</td><td class="desc-cell">Employment Situation</td></tr></table>'
        self.assertEqual(list(monthly_events(html,date(2026,12,1),"BLS"))[0]["scheduled_at"],"2026-12-04T13:30:00+00:00")

    def test_fed_monthly_time_used_only_for_requested_month(self):
        html = ('<div class="row-title"><h4>December 2026</h4></div><div class="panel-unstyled"><div class="panel-body">'
                '<div class="row"><div class="col-xs-2"><p>2:30 p.m.</p></div><div class="col-xs-7"><p>FOMC Press Conference</p></div>'
                '<div class="col-xs-3"><p>9</p></div></div></div></div>')
        row, = monthly_events(html,date(2026,12,1),"FED")
        self.assertEqual((row["kind"],row["scheduled_at"]),("FOMC_PRESS","2026-12-09T19:30:00+00:00"))
        self.assertFalse(list(monthly_events(html,date(2027,1,1),"FED")))

    def test_countertrend_v2_without_macro(self):
        frame={"end":14400,"momentum":-3,"trend":5,"move":-1.2,"acceleration":-1,"volume":1.5,"atr":2,
               "atr_ratio":1.1,"rsi":45,"close":3500,"recent_low":3498,"recent_high":3504,"support":3480,
               "resistance":3515,"bar":[0,3501,3502,3499,3500,2],"prev":[0,3503,3505,3501,3504,3],
               "ema":3501,"ema200":3500}
        frames=dict.fromkeys(("1m","5m","15m","1H","4H"),frame)
        out=evaluate(frames,14401)
        self.assertEqual(out["scenarios"]["5–15 min"],"SHORT")
        self.assertEqual(out["scenarios"]["1–6 ore"],"LONG")
        s=out["strategies"]["TECHNICAL_V2_SCALP"]
        self.assertEqual((s["candidate"],s["status"]),("SHORT","CONDITIONAL_WATCH"))
        self.assertLess(s["plan"]["tp3"],s["plan"]["entry_limit"])
        self.assertGreater(s["plan"]["stop"],s["plan"]["entry_limit"])
        book={"ts":"14401000","bids":[["3502"]],"asks":[["3502.1"]]}
        quote={"book":book,"connected":True,"observed_at":14401}
        s=evaluate(frames,14401,quote)["strategies"]["TECHNICAL_V2_SCALP"]
        self.assertIn("limit_already_marketable",s["blockers"])
        self.assertNotIn("plan",s)

    def test_stale_ws_and_fresh_rest_are_distinguished(self):
        with tempfile.TemporaryDirectory() as path:
            root=Path(path)
            book={"ts":"1000000","bids":[["100"]],"asks":[["101"]]}
            save(root/"live_quote.json",{"observed_at":992,"connected":True,"book":book})
            save(root/"rest_quote.json",{"book":book})
            self.assertEqual(snapshot(root,1001)["book_source"],"OKX_EEA_REST")
            self.assertIsNone(snapshot(root,1009)["book"])

    def test_register_filters_and_cohorts(self):
        one=setup()
        one["observed_at"]=100000
        two=dict(one,direction="SHORT",cohort="CONDITIONAL_WATCH")
        value=paper_page({"a":one,"b":two},{"direction":["SHORT"],"period":["24h"]},100100)
        self.assertEqual(value["total"],1)
        self.assertEqual(value["options"]["timeframe"],["1m"])
        self.assertEqual(list(value["metrics"]),["TECHNICAL:CONDITIONAL_WATCH"])

    def test_setup_and_targets_have_stable_notifications(self):
        item=setup()
        item.update(schema=2,state="STOP",filled_at=120,ended_at=240,targets_touched={"tp1":180})
        result=list(events({"key":item},{},250))
        self.assertEqual([x[0] for x in result],["key:setup","key:entry","key:tp1","key:end"])
        self.assertIn("attraversata",result[1][2])

    def test_endpoint_contract(self):
        with tempfile.TemporaryDirectory() as path:
            root=Path(path)
            now=time.time()
            save(root/"dashboard.json",{"generated_at":now,"research_only":True})
            save(root/"paper_ledger.json",{"a":dict(setup(),observed_at=now)})
            with socket.socket() as s:
                s.bind(("127.0.0.1",0))
                port=s.getsockname()[1]
            base=f"http://127.0.0.1:{port}"
            threading.Thread(target=serve,args=(root,port),daemon=True).start()
            for _ in range(40):
                try:
                    with urlopen(base+"/healthz",timeout=1) as r:
                        self.assertEqual(r.status,200)
                    break
                except OSError:
                    time.sleep(.03)
            req=Request(base+"/dashboard.json",headers={"Accept-Encoding":"gzip"})
            with urlopen(req) as r:
                self.assertTrue(json.loads(gzip.decompress(r.read()))["research_only"])
            with urlopen(base+"/paper.json?period=24h&direction=LONG") as r:
                self.assertEqual(json.load(r)["total"],1)
            for path,status in (("/live.json",503),("/paper_ledger.json",404),("/paper.json?period=invalid",400)):
                with self.assertRaises(HTTPError) as exc:
                    urlopen(base+path)
                self.assertEqual(exc.exception.code,status)


if __name__ == "__main__":
    unittest.main()
