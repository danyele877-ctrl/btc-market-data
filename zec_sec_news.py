"""Official Strategy filing context for the radar, independent of signal decisions."""

import json
import re
import time
from datetime import timedelta
from html import unescape
from io import BytesIO
from urllib.request import Request, urlopen

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from render_bridge_publication import BTC_ID, _timestamp
from zec_macro_shadow import ZEC_STREAM, minute_bars

INVESTOR_RELATIONS = "https://www.strategy.com/investor-relations"
PDF_URL = re.compile(r"https://assets\.contentstack\.io/v3/assets/bltf8d808d9b8cebd37/"
                     r"[a-z0-9]+/[a-z0-9]+/[A-Za-z0-9_.-]+\.pdf")
POLL_SECONDS = 60
LOOKBACK = timedelta(minutes=15)
MAX_RESPONSE = 4_000_000


def recent_filings(page, now):
    data = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                     page, re.DOTALL)
    if not data:
        raise ValueError("Strategy structured investor-relations data missing")
    groups = json.loads(data[1])["props"]["pageProps"]["taxonomyCardListData"]
    for group in groups:
        for item in group.get("sec_documents", ()):
            date = item.get("published_date", "")
            url = item.get("file_reference", {}).get("url", "")
            uid = item.get("uid", "")
            if (not re.match(r"Form 8-K(?: |$)", item.get("title", ""))
                    or not re.fullmatch(r"blt[0-9a-f]{16}", uid)
                    or not PDF_URL.fullmatch(url)
                    or not re.search(r"T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$", date)):
                continue
            published = _timestamp(date)
            if now - LOOKBACK <= published <= now:
                yield uid, published, url


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
        self.next_poll = 0
        self.checked = {}

    def download(self, url):
        request = Request(url, headers={"User-Agent": "BTC-ZEC-Radar/1.0"})
        with urlopen(request, timeout=4) as response:
            data = response.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE:
            raise ValueError("Strategy response exceeds size limit")
        return data

    def poll(self, now):
        if time.monotonic() < self.next_poll:
            return
        self.next_poll = time.monotonic() + POLL_SECONDS
        self.checked = {key: value for key, value in self.checked.items()
                        if now - value[0] <= LOOKBACK}
        try:
            page = self.download(INVESTOR_RELATIONS).decode("utf-8")
            for uid, published, url in recent_filings(page, now):
                if uid not in self.checked:
                    pdf = PdfReader(BytesIO(self.download(url)))
                    label = material_change(" ".join(page.extract_text() for page in pdf.pages[:8]))
                    self.checked[uid] = (published, url, label)
        except (OSError, ValueError, KeyError, TypeError, PyPdfError) as exc:
            self.next_poll = time.monotonic() + 5 * POLL_SECONDS
            print(f"Strategy investor-relations feed unavailable ({type(exc).__name__})", flush=True)

    def events(self, now):
        return [(uid, published, url, label)
                for uid, (published, url, label) in self.checked.items()
                if label and now - LOOKBACK <= published <= now]
