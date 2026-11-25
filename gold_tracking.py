"""Local research ledger; no execution, no retroactive entries."""
import json, math, os
from pathlib import Path
from datetime import datetime, timezone
from gold_history import INST

HORIZONS=(1,4,6,12,24)

def record(ledger,snapshot,now):
    if snapshot.get("instrument")!=INST or snapshot.get("version")!="hybrid-shadow-v3":
        raise ValueError("unexpected snapshot")
    captured=datetime.fromisoformat(snapshot["captured_at"]).timestamp()
    if not 0<=now-captured<=600: raise ValueError("snapshot not contemporaneous")
    diag=snapshot.get("trigger_diagnostics",{})
    if snapshot.get("trigger") not in ("BREAKOUT","PULLBACK","BOTH") or not snapshot.get("execution_feed_ready") or snapshot.get("blockers") or snapshot.get("trigger_blockers"):
        return False
    direction=snapshot["candidate"]
    if direction not in ("LONG","SHORT"): raise ValueError("direction")
    key=f'{snapshot["version"]}:{diag["bar_close_ts"]}'
    if key in ledger: return False
    ledger[key]={"observed_at":now,"direction":direction,"trigger":snapshot["trigger"],"entry":None,"outcomes":{}}
    return True

def settle(ledger,rows,now):
    bars={int(r[0])//1000:r for r in rows if r[8]=="1" and int(r[0])/1000+300<=now}
    for item in ledger.values():
        start=(int(item["observed_at"])//300+1)*300
        if item["entry"] is None and start in bars:
            item["entry"]={"ts":start,"price":float(bars[start][1])}
        if item["entry"] is None: continue
        for h in HORIZONS:
            key=str(h); end=start+h*3600
            if key in item["outcomes"] or now<end or end-300 not in bars: continue
            price=float(bars[end-300][4]); entry=item["entry"]["price"]
            if min(price,entry)<=0 or not all(map(math.isfinite,(price,entry))): continue
            gross=(1 if item["direction"]=="LONG" else -1)*(price/entry-1)
            item["outcomes"][key]={"end_ts":end,"gross_return":gross,"net_fee_only":gross-.001}
    return ledger

def save(path,ledger):
    path=Path(path); tmp=path.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger,indent=2,allow_nan=False)); os.replace(tmp,path)
