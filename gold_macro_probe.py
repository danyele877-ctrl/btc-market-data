"""Research-only connectivity probe; no trading signals."""
import json, math, urllib.request, concurrent.futures, time
from datetime import datetime, timezone
from pathlib import Path

SOURCES = {
    "dxy": "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?range=5d&interval=1h",
    "gold_futures_proxy": "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?range=5d&interval=1h",
    "treasury_nominal": "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value_month="+datetime.now(timezone.utc).strftime("%Y%m"),
    "treasury_real": "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?data=daily_treasury_real_yield_curve&field_tdr_date_value_month="+datetime.now(timezone.utc).strftime("%Y%m"),
    "fed_calendar": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
    "bls_calendar": "https://www.bls.gov/schedule/news_release/bls.ics",
    "bea_calendar": "https://www.bea.gov/news/schedule",
    "bls_series": "https://api.bls.gov/publicAPI/v2/timeseries/data/CUUR0000SA0"
}

def probe(item):
    name,url=item
    start=time.monotonic()
    out={"url":url,"retrieved_at":datetime.now(timezone.utc).isoformat(),"validated":False}
    try:
        req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0 GoldRadarResearch/1.0","Accept":"*/*"})
        with urllib.request.urlopen(req,timeout=20) as r:
            body=r.read(2000001)
            out.update(status=r.status,content_type=r.headers.get("Content-Type"),bytes=len(body))
        if len(body)>2000000: raise ValueError("response too large")
        if name in ("dxy","gold_futures_proxy"):
            obj=json.loads(body)["chart"]["result"][0]
            ts=obj["timestamp"]; prices=obj["indicators"]["quote"][0]["close"]
            valid=sorted({int(t): float(p) for t,p in zip(ts,prices) if p is not None and math.isfinite(p) and p>0}.items())
            out["timeliness"]="DELAYED"
            out["provider"]="Yahoo Finance"
            out["last_observation"]=valid[-1]
            out["symbol"]=obj["meta"]["symbol"]
            expected={"dxy":"DX-Y.NYB","gold_futures_proxy":"GC=F"}[name]
            now=datetime.now(timezone.utc).timestamp()
            closed=[(t,p) for t,p in valid if t+3600<=now and p>0]
            if out["symbol"]!=expected or len(closed)<2: raise ValueError("symbol or history invalid")
            out["identity_verified"]=True
            out["closed_observations"]=closed[-25:]
            out["age_seconds"]=now-closed[-1][0]-3600
            out["hourly_change_pct"]=100*(closed[-1][1]/closed[-2][1]-1) if closed[-1][0]-closed[-2][0]==3600 else None
            out["validated"]=0<=out["age_seconds"]<=7200 and out["hourly_change_pct"] is not None
        elif name=="bls_series":
            obj=json.loads(body)
            if obj["status"]!="REQUEST_SUCCEEDED": raise ValueError(obj["status"])
            out["latest"]=obj["Results"]["series"][0]["data"][0]
        elif name=="bls_calendar":
            out["calendar_events"]=body.count(b"BEGIN:VEVENT")
        elif name.startswith("treasury_"):
            import xml.etree.ElementTree as ET
            root=ET.fromstring(body)
            entries=root.findall(".//{http://www.w3.org/2005/Atom}entry")
            out["entries"]=len(entries)
            field="BC_10YEAR" if name=="treasury_nominal" else "TC_10YEAR"
            records=[]
            for entry in entries:
                values={node.tag.split("}")[-1]:node.text for node in entry.iter()}
                if values.get("NEW_DATE") and values.get(field):
                    records.append((values["NEW_DATE"],float(values[field])))
            out["daily_10y"]=sorted(records)[-5:]
            out["frequency"]="daily"
            out["timeliness"]="DAILY"
            out["daily_available"]=bool(records)
            out["validated"]=False
        out["transport_ok"]=True
    except Exception as exc:
        out["error"]=str(exc)
    out["elapsed_seconds"]=round(time.monotonic()-start,3)
    return name,out

if __name__=="__main__":
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        result=dict(pool.map(probe,SOURCES.items()))
    Path("gold-macro-probe.json").write_text(json.dumps({"research_only":True,"sources":result},indent=2))


def macro_for_display(current, previous):
    """Keep last verified readings for presentation while probes are unavailable."""
    display = {}
    for name in ("dxy", "gold_futures_proxy", "treasury_nominal", "treasury_real"):
        row = current.get(name, {})
        if ((row.get("identity_verified") and row.get("closed_observations"))
                or row.get("daily_available")):
            display[name] = row
        elif previous.get(name):
            display[name] = dict(previous[name], cached=True)
    return display


def aligned_hour(macro, now, max_age=5400):
    """Compare matched closed proxy intervals, keeping vendor timestamps intact."""
    from math import isfinite
    series = []
    for key in ("dxy", "gold_futures_proxy"):
        source = macro.get(key, {})
        if not source.get("validated") and not source.get("identity_verified"):
            return None
        rows = source.get("closed_observations", [])
        series.append({int(t): p for t, p in rows if p > 0 and isfinite(p)})
    common = series[0].keys() & series[1].keys()
    eligible = [t for t in common if t-3600 in common and 0 <= now-(t+3600) <= max_age]
    if not eligible:
        return None
    anchor = max(eligible)
    return dict(start=anchor, end=anchor+3600,
                dxy_return=series[0][anchor]/series[0][anchor-3600]-1,
                gc_return=series[1][anchor]/series[1][anchor-3600]-1,
                timeliness="DELAYED")


def xau_same_hour(rows, interval):
    from gold_history import BARS
    start, end = interval["start"], interval["end"]
    records = {}
    for row in rows:
        stamp = int(row[0])
        if stamp % BARS["1m"] or row[8] != "1" or not start*1000 <= stamp < end*1000:
            continue
        stamp //= 1000
        if stamp in records and records[stamp] != row:
            return None
        records[stamp] = row
    if any(t not in records for t in range(start,end,60)):
        return None
    o, c = float(records[start][1]), float(records[end-60][4])
    from math import isfinite
    if not (0 < o and 0 < c and isfinite(o) and isfinite(c)):
        return None
    return c/o-1
