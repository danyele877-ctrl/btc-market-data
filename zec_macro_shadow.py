"""Read-only event diagnostics using closed, aligned OKX Europe candles."""

from datetime import datetime
from math import isfinite
from typing import NamedTuple

from render_bridge_publication import BTC_ID, ZEC_ID, _timestamp

ZEC_STREAM = "ZEC-USD_UM_XPERP"
PRE_MINUTES = 10
PAUSE_MINUTES = 2
WATCH_MINUTES = 25
PLAN_VERSION = 1
FED_NOVEMBER = "https://www.federalreserve.gov/newsevents/2026-november.htm"
FED_DECEMBER = "https://www.federalreserve.gov/newsevents/2026-december.htm"
BLS_CPI = "https://www.bls.gov/schedule/news_release/cpi.htm"


class Event(NamedTuple):
    id: str
    title: str
    utc: str
    source: str

    @property
    def at(self):
        return int(datetime.fromisoformat(self.utc).timestamp())


EVENTS = (
    Event("us-cpi-2026-11-10", "Inflazione USA (CPI)", "2026-11-10T13:30:00+00:00", BLS_CPI),
    Event("fed-minutes-2026-11-18", "Verbali FOMC", "2026-11-18T19:00:00+00:00", FED_NOVEMBER),
    Event("fed-waller-2026-11-20", "Intervento Fed · Waller", "2026-11-20T13:25:00+00:00", FED_NOVEMBER),
    Event("fed-cook-2026-11-20", "Intervento Fed · Cook", "2026-11-20T17:15:00+00:00", FED_NOVEMBER),
    Event("fomc-2026-12-09", "Decisione FOMC", "2026-12-09T19:00:00+00:00", FED_DECEMBER),
    Event("fomc-presser-2026-12-09", "Conferenza stampa FOMC", "2026-12-09T19:30:00+00:00", FED_DECEMBER),
    Event("us-cpi-2026-12-10", "Inflazione USA (CPI)", "2026-12-10T13:30:00+00:00", BLS_CPI),
    Event("fed-minutes-2026-12-30", "Verbali FOMC", "2026-12-30T19:00:00+00:00", FED_DECEMBER),
)


def minute_bars(rows, start, end):
    bars = {}
    for row in rows:
        try:
            closed = _timestamp(row["completed_at"]).timestamp()
            low, high, close, opening = (float(row[key]) for key in ("low", "high", "close", "open"))
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if start <= closed <= end and 0 < low <= min(opening, close) <= max(opening, close) <= high and isfinite(high):
            bars[closed] = low, high, close, opening
    return bars


def event_context(event, btc_rows, zec_rows, now):
    """Find the first aligned break and first retest using only known closes."""
    timestamp = now.timestamp()
    at = event.at
    start = at - PRE_MINUTES * 60
    last = min(int(timestamp) // 60 * 60, at + WATCH_MINUTES * 60)
    result = {
        "version": PLAN_VERSION, "mode": "SHADOW", "event": event._asdict(),
        "instruments": [BTC_ID, ZEC_ID],
        "pause_from": start, "pause_until": at + PAUSE_MINUTES * 60,
        "watch_until": at + WATCH_MINUTES * 60,
        "observed_at": timestamp, "status": "waiting",
    }
    if timestamp < at + PAUSE_MINUTES * 60:
        return {**result, "status": "macro_pause"}

    times = range(start + 60, last + 1, 60)
    btc = minute_bars(btc_rows, start + 60, last)
    zec = minute_bars(zec_rows, start + 60, last)
    if any(t not in btc or t not in zec for t in times):
        return {**result, "status": "missing_candles"}

    before = range(start + 60, at + 1, 60)
    btc_low, btc_high = min(btc[t][0] for t in before), max(btc[t][1] for t in before)
    zec_low, zec_high = min(zec[t][0] for t in before), max(zec[t][1] for t in before)
    if btc_low == btc_high or zec_low == zec_high:
        return {**result, "status": "flat_reference_range"}

    for t in range(at + PAUSE_MINUTES * 60, last + 1, 60):
        btc_close, zec_close = btc[t][2], zec[t][2]
        relative = zec_close / zec[at][2] / (btc_close / btc[at][2]) - 1
        if btc_close < btc_low and zec_close < zec_low and relative < 0:
            side, sign, level, btc_level, stop = "SHORT", -1, zec_low, btc_low, zec_high
        elif btc_close > btc_high and zec_close > zec_high and relative > 0:
            side, sign, level, btc_level, stop = "LONG", 1, zec_high, btc_high, zec_low
        else:
            continue
        result.update(status="watching", side=side, watch_at=t, breakout_level=level,
                      relative_zec_btc_pct=round(relative * 100, 4))
        for retest in range(t + 60, last + 1, 60):
            low, high, close, _ = zec[retest]
            if sign * (btc[retest][2] - btc_level) <= 0 or sign * (close - level) <= 0:
                return {**result, "status": "breakout_reabsorbed", "reabsorbed_at": retest}
            if low <= level <= high:
                return {**result, "status": "retest_observed", "retest_at": retest,
                        "reference_limit": level, "reference_stop": stop}
        return result
    return result


def live_macro_context(instruments, now):
    timestamp = now.timestamp()
    for event in reversed(EVENTS):
        if event.at - PRE_MINUTES * 60 <= timestamp < event.at + WATCH_MINUTES * 60:
            return event_context(event, instruments.get(BTC_ID, {}).get("1m", ()),
                                 instruments.get(ZEC_STREAM, {}).get("1m", ()), now)
    upcoming = next((event._asdict() for event in EVENTS if event.at > timestamp), None)
    return {"mode": "SHADOW", "status": "outside_window" if upcoming else "calendar_exhausted",
            "next_event": upcoming}
