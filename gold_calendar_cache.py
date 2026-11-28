"""Research calendar cache; unverified timestamps never generate countdowns."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from gold_calendar_sources import probe, SOURCES
from gold_calendar import fetch
from gold_calendar_unified import monthly_events, normalize
from gold_tracking import save

SCHEMA = 3


def enrich(result, now):
    start = now.date().replace(day=1)
    following = (start + timedelta(days=32)).replace(day=1)
    meetings = {e["date"]: e for e in result["events"] if e["kind"] == "FOMC"}
    bls_keys = {(e["kind"], datetime.fromisoformat(e["scheduled_at"]).strftime("%Y-%m"))
                for e in result["events"] if e["source"] == "BLS"}
    jobs = []
    for month in (start, following):
        if any(day.startswith(month.isoformat()[:7]) for day in meetings):
            jobs.append(("FED", month, f"https://www.federalreserve.gov/newsevents/{month:%Y-%B}.htm".lower()))
        if not all((kind, month.strftime("%Y-%m")) in bls_keys for kind in ("CPI", "NFP")):
            jobs.append(("BLS", month, f"https://www.bls.gov/schedule/{month:%Y/%m}_sched_list.htm"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [(source, month, url, pool.submit(probe, (source, url))) for source, month, url in jobs]
        for source, month, url, future in futures:
            raw = future.result()[1].get("source_html", "")
            for event in monthly_events(raw, month, source):
                event["source_url"] = url
                if source == "BLS":
                    key = event["kind"], month.strftime("%Y-%m")
                    if key not in bls_keys:
                        result["events"].append(event)
                        bls_keys.add(key)
                        if result["coverage"]["BLS"] == "UNAVAILABLE":
                            result["coverage"]["BLS"] = "MONTHLY_FALLBACK"
                elif event["date"] in meetings:
                    if event["kind"] == "FOMC":
                        meetings[event["date"]].update(event)
                    else:
                        result["events"].append(event)
    return result


def refresh(root, now=None):
    now = now or datetime.now(timezone.utc)
    path = Path(root) / "calendar.json"
    old = json.loads(path.read_text()) if path.exists() else {}
    try:
        age = (now-datetime.fromisoformat(old["retrieved_at"])).total_seconds()
    except (KeyError, ValueError, TypeError):
        age = float("inf")
    if old.get("schema_version") != SCHEMA or not 0 <= age < 3600:
        with ThreadPoolExecutor(max_workers=3) as pool:
            bls = pool.submit(fetch, now)
            sources = dict(pool.map(probe, SOURCES.items()))
            old = enrich(normalize(sources, now, bls.result()), now)
        old.update(retrieved_at=now.isoformat(), schema_version=SCHEMA)
        save(path, old)
    result = dict(old, events=[])
    for event in old.get("events", []):
        e = dict(event)
        e.pop("seconds_to_event", None)
        e.pop("phase", None)
        if e.get("status") == "TIMESTAMP_PARSED" and e.get("scheduled_at"):
            delta = (datetime.fromisoformat(e["scheduled_at"]) - now).total_seconds()
            if delta < -3600:
                continue
            e["seconds_to_event"] = round(delta)
            e["phase"] = "POST_RELEASE" if delta<=0 else "IMMINENT" if delta<=1800 else "UPCOMING"
        elif e.get("date") and e["date"] < now.date().isoformat():
            continue
        result["events"].append(e)
    save(Path(root)/"calendar_view.json",result)
    return result
