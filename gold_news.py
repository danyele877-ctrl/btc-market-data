"""Source-attributed macro and geopolitical headlines. No directional inference."""
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from gold_tracking import save

SOURCES = {
    "Federal Reserve": "https://www.federalreserve.gov/feeds/press_monetary.xml",
    "BBC World": "https://feeds.bbci.co.uk/news/world/rss.xml",
}
IMPACT = re.compile(r"tariff|sanctions?|ceasefire|missile|blockade|hormuz|airstrike|invasi|central bank|inflation", re.I)


def collect(name, url, now):
    req = Request(url, headers={"User-Agent": "GoldRadarResearch/1.0", "Accept": "application/rss+xml, application/xml"})
    with urlopen(req, timeout=12) as response:
        body = response.read(1000001)
    if len(body)>1000000:
        raise ValueError("News feed exceeds size bound")
    rows = []
    for item in ElementTree.fromstring(body).findall(".//item"):
        title, link, stamp = (item.findtext(key, "").strip() for key in ("title","link","pubDate"))
        if not title or not link.startswith("https://") or name != "Federal Reserve" and not IMPACT.search(title):
            continue
        try:
            published = parsedate_to_datetime(stamp)
            if published.tzinfo is None:
                continue
            published_at = published.timestamp()
        except (TypeError, ValueError, OverflowError):
            continue
        if 0 <= now - published_at <= 3*86400:
            rows.append(dict(source=name,title=title[:280],url=link,published_at=published_at,
                             official=name=="Federal Reserve"))
    return rows


def refresh(root, now=None):
    now = time.time() if now is None else now
    items, coverage = [], {}
    with ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
        for name, task in {name:pool.submit(collect,name,url,now) for name,url in SOURCES.items()}.items():
            try:
                items.extend(task.result())
                coverage[name]="AVAILABLE"
            except Exception as exc:
                logging.info("News feed %s unavailable (%s)", name, type(exc).__name__)
                coverage[name]="UNAVAILABLE"
    result = dict(captured_at=now,coverage=coverage,
                  items=sorted({item["url"]:item for item in items}.values(),key=lambda x:x["published_at"],reverse=True)[:12])
    save(Path(root)/"news.json",result)
    return result
