"""Research-only connectivity probe; no trading signals."""
import json, urllib.request, concurrent.futures, time
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
            valid=[(t,p) for t,p in zip(ts,prices) if p is not None]
            out["last_observation"]=valid[-1]
            out["symbol"]=obj["meta"]["symbol"]
        elif name=="bls_series":
            obj=json.loads(body)
            if obj["status"]!="REQUEST_SUCCEEDED": raise ValueError(obj["status"])
            out["latest"]=obj["Results"]["series"][0]["data"][0]
        elif name=="bls_calendar":
            out["calendar_events"]=body.count(b"BEGIN:VEVENT")
        elif name.startswith("treasury_"):
            import xml.etree.ElementTree as ET
            root=ET.fromstring(body)
            out["entries"]=len(root.findall(".//{http://www.w3.org/2005/Atom}entry"))
        out["transport_ok"]=True
    except Exception as exc:
        out["error"]=str(exc)
    out["elapsed_seconds"]=round(time.monotonic()-start,3)
    return name,out

if __name__=="__main__":
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        result=dict(pool.map(probe,SOURCES.items()))
    Path("gold-macro-probe.json").write_text(json.dumps({"research_only":True,"sources":result},indent=2))
