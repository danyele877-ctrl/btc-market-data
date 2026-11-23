"""Official Strategy filing context for the radar, independent of signal decisions."""

import json
import os
import re
import time
from datetime import timedelta
from html import unescape
from urllib.request import Request, urlopen

from render_bridge_publication import BTC_ID, _timestamp
from zec_macro_shadow import ZEC_STREAM, minute_bars

SUBMISSIONS = "https://data.sec.gov/submissions/CIK0001050446.json"
ARCHIVES = "https://www.sec.gov/Archives/edgar/data/1050446"
POLL_SECONDS = 60
LOOKBACK = timedelta(minutes=15)
MAX_RESPONSE = 4_000_000


def recent_filings(payload, now):
    recent = payload["filings"]["recent"]
    for form, accepted, accession, document, items in zip(
        recent["form"], recent["acceptanceDateTime"], recent["accessionNumber"],
        recent["primaryDocument"], recent["items"],
    ):
        if (form != "8-K" or not ({"7.01", "8.01"} & set(re.split(r"[,\s]+", items)))
                or not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accession)
                or not re.fullmatch(r"[A-Za-z0-9_.-]+\.html?", document)):
            continue
        accepted = _timestamp(accepted)
        if now - LOOKBACK <= accepted <= now:
            yield accession, accepted, f"{ARCHIVES}/{accession.replace('-', '')}/{document}"


def material_change(document):
    text = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", document))).lower()
    # Require explicit policy language, not reported purchases or holdings alone.
    if re.search(r"approved (?:an increase in|a decrease in|a new) [^.]{0,160}"
                 r"repurchase program", text):
        return "Modifica al programma di riacquisto"
    if re.search(r"authorized management to (?:reallocate|transfer) [^.]{0,180}usd reserve", text):
        return "Modifica autorizzata alla riserva in dollari"
    return None


def market_reactions(instruments, published_at, now):
    # Start at the first full minute after publication. An aligned minute keeps its opening.
    start = -(-published_at.timestamp() // 60) * 60
    btc, zec = (minute_bars(instruments.get(key, {}).get("1m", ()), start+60, start+300)
                for key in (BTC_ID, ZEC_STREAM))
    reactions = {}
    for minutes in (1, 5):
        end = start + minutes * 60
        if now.timestamp() < end:
            continue
        if any(t not in btc or t not in zec for t in range(int(start)+60, int(end)+1, 60)):
            continue
        reactions[minutes] = ((btc[end][2] / btc[start+60][3] - 1) * 100,
                              (zec[end][2] / zec[start+60][3] - 1) * 100)
    return reactions


class StrategyNews:
    def __init__(self):
        contact = os.getenv("RADAR_SEC_CONTACT_EMAIL", "").strip()
        self.user_agent = f"BTC ZEC Radar {contact}" if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", contact) else ""
        self.next_poll = 0
        self.checked = {}

    def download(self, url):
        request = Request(url, headers={"User-Agent": self.user_agent})
        with urlopen(request, timeout=4) as response:
            data = response.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE:
            raise ValueError("SEC response exceeds size limit")
        return data

    def poll(self, now):
        if not self.user_agent or time.monotonic() < self.next_poll:
            return
        self.next_poll = time.monotonic() + POLL_SECONDS
        self.checked = {key: value for key, value in self.checked.items()
                        if now - value[0] <= LOOKBACK}
        try:
            payload = json.loads(self.download(SUBMISSIONS))
            for accession, accepted, url in recent_filings(payload, now):
                if accession not in self.checked:
                    label = material_change(self.download(url).decode("utf-8", errors="replace"))
                    self.checked[accession] = (accepted, url, label)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.next_poll = time.monotonic() + 5 * POLL_SECONDS
            print(f"SEC Strategy feed unavailable ({type(exc).__name__})", flush=True)

    def events(self, now):
        return [(accession, accepted, url, label)
                for accession, (accepted, url, label) in self.checked.items()
                if label and now - LOOKBACK <= accepted <= now]
