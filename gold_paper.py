"""Research only. Candle touches are not exchange fills; no orders."""
import json,math
from pathlib import Path
from collections import Counter
from gold_tracking import save

def advance(item,bars,now):
    p=item["plan"]; d=1 if item["direction"]=="LONG" else -1
    step=item["step"]; cursor=item["cursor"]
    for b in sorted(bars,key=lambda x:x[0]):
        t,o,h,l,c,v=b
        if t<cursor or t+step>now: continue
        if t!=cursor:
            item["state"]="DATA_GAP"; return
        if not all(math.isfinite(x) for x in b) or min(o,h,l,c)<=0 or v<0:
            item["state"]="DATA_GAP"; return
        if item["state"]=="PENDING" and t+step>item["expires_at"]:
            item["state"]="EXPIRED"; return
        if item["state"]=="TOUCHED" and t+step>item["filled_at"]+item["holding_seconds"]:
            item["state"]="TIMEOUT"; return
        stop=l<=p["stop"] if d==1 else h>=p["stop"]
        entry=l<p["entry_limit"] if d==1 else h>p["entry_limit"]
        target=h>=p["tp1"] if d==1 else l<=p["tp1"]
        item["cursor"]=t+step
        if v<=0: continue
        if item["state"]=="PENDING":
            if entry:
                item["filled_at"]=t+step
                item["state"]="STOP_OR_AMBIGUOUS" if stop else "TOUCHED"
                if stop: return
                # Do not credit targets in entry candle: sequence unknown.
            elif stop:
                item["state"]="INVALIDATED"; return
        elif item["state"]=="TOUCHED":
            if stop: item["state"]="STOP_OR_AMBIGUOUS"; return
            if target: item["state"]="TP1_TOUCHED"; return
    if item["state"]=="PENDING" and now>=item["expires_at"] and item["cursor"]>=item["expires_at"]:
        item["state"]="EXPIRED"

def update(root,technical,swing,market,now):
    path=Path(root)/"paper_ledger.json"
    ledger=json.loads(path.read_text()) if path.exists() else {}
    series=market.get("series",{})
    for item in ledger.values():
        if item["state"] in ("PENDING","TOUCHED"):
            bars=series.get(item["bar"],{}).get("bars",[])
            advance(item,bars,now)
    # One immutable proposal per closed reference bar and model, no retroactive registrations.
    def add(model,direction,p,bar,ref,validity,holding,cohort,blockers):
        if direction not in ("LONG","SHORT") or not p: return
        if not all(k in p for k in ("entry_limit","stop","tp1")): return
        key=f"{model}:{int(ref)}:{direction}"
        if key in ledger: return
        if any(x["model"]==model and x["direction"]==direction and x["state"] in ("PENDING","TOUCHED") for x in ledger.values()): return
        step=60 if bar=="1m" else 300
        start=(int(now)//step+1)*step
        ledger[key]={"model":model,"direction":direction,"plan":p,"observed_at":now,"expires_at":start+validity,"holding_seconds":holding,"state":"PENDING","cursor":start,"step":step,"bar":bar,"cohort":cohort,"blockers":blockers}
    if technical.get("status") in ("PAPER_TEST_CANDIDATE","CONDITIONAL_WATCH") and series.get("15m"):
        add("TECHNICAL",technical["candidate"],technical.get("scenario"),"1m",series["15m"]["bar_end"],300,21600,technical["status"],technical.get("blockers",[]))
    if swing.get("candidate") in ("LONG","SHORT") and series.get("4H"):
        direction=swing["candidate"]
        add("SWING",direction,swing.get("plans",{}).get(direction),"5m",series["4H"]["bar_end"],21600,259200,"STRUCTURAL_ONLY",["funding_unverified","macro_unverified"])
    save(path,ledger)
    counts={}
    for x in ledger.values():
        k=x["model"]+":"+x["cohort"]
        counts.setdefault(k,Counter())[x["state"]]+=1
    return {"research_only":True,"fill_verified":False,"counts":{k:dict(v) for k,v in counts.items()},"recent":sorted(ledger.values(),key=lambda x:x["observed_at"],reverse=True)[:12]}
