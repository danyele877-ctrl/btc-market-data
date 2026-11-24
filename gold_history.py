"""Research-only closed candle backfill. No orders, signals or credentials."""
import argparse
import json
import math
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://eea.okx.com/api/v5"
INST = "XAU-USD_UM_XPERP-310502"
BARS = {"1m": 60000, "5m": 300000, "15m": 900000,
        "1H": 3600000, "4H": 14400000, "1Dutc": 86400000}

def request(path, **params):
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "GoldRadarDiagnostic/1.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as response:
                obj = json.load(response)
            if obj.get("code") != "0" or not isinstance(obj.get("data"), list):
                raise ValueError(str(obj)[:300])
            return obj["data"]
        except (OSError, ValueError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)

def metadata():
    rows = request("/public/instruments", instType="FUTURES", instId=INST)
    matches = [r for r in rows if r.get("instId") == INST and r.get("instType") == "FUTURES"]
    if len(matches) != 1 or matches[0].get("state") != "live":
        raise ValueError("Exact live EEA instrument not verified")
    return matches[0]

def collect(bar, days, fetch=request, now=None):
    now = int(time.time()*1000) if now is None else now
    cutoff = now - days*86400000
    cursor = None
    records = {}
    step = BARS[bar]
    max_pages = math.ceil(days*86400000/step/100)+5
    exhausted = False
    for _ in range(max_pages):
        params = dict(instId=INST, bar=bar, limit="100")
        if cursor is not None:
            params["after"] = str(cursor)
        rows = fetch("/market/history-candles", **params)
        if not rows:
            exhausted = True
            break
        oldest = min(int(r[0]) for r in rows)
        if cursor is not None and oldest >= cursor:
            raise ValueError("Pagination did not advance")
        for r in rows:
            if len(r) < 9:
                raise ValueError("Malformed candle")
            ts = int(r[0])
            if r[8] != "1" or ts < cutoff or ts+step > now:
                continue
            vals = [float(v) for v in r[1:8]]
            o,h,l,c,vol,*_ = vals
            if not all(math.isfinite(v) for v in vals) or min(o,h,l,c)<=0 or h<max(o,l,c) or l>min(o,h,c) or min(vals[4:])<0:
                raise ValueError("Invalid OHLCV")
            if ts in records and records[ts] != r:
                raise ValueError("Conflicting duplicate")
            records[ts] = r
        if oldest <= cutoff:
            exhausted = True
            break
        cursor = oldest
        time.sleep(.25)
    candles = [records[k] for k in sorted(records)]
    gaps = sum(max(0,(int(b[0])-int(a[0]))//step-1) for a,b in zip(candles,candles[1:]))
    return {"candles": candles, "count":len(candles), "missing_intervals":gaps,
            "zero_volume":sum(float(r[5])==0 for r in candles),
            "pagination_finished":exhausted,
            "first_ts":int(candles[0][0]) if candles else None,
            "last_ts":int(candles[-1][0]) if candles else None,
            "last_close_age_ms":now-int(candles[-1][0])-step if candles else None}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="gold-history.json")
    args = parser.parse_args()
    meta = metadata()
    now = int(time.time()*1000)
    windows = {"1m":2,"5m":14,"15m":30,"1H":90,"4H":180,"1Dutc":365}
    result = {"instrument":INST,"source":BASE,"captured_ms":now,"metadata":meta,
              "signals_enabled":False,"orders_enabled":False,"sizing_enabled":False,"series":{}}
    for bar,days in windows.items():
        result["series"][bar] = collect(bar,days,now=now)
    path = Path(args.output)
    tmp = path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(result,separators=(",",":")))
    os.replace(tmp,path)
    print(json.dumps({k:{a:b for a,b in v.items() if a!="candles"} for k,v in result["series"].items()},indent=2))

if __name__ == "__main__":
    main()
