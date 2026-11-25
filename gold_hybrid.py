"""Prospective research snapshot; never an executable signal."""
import json, time, math
from pathlib import Path
from datetime import datetime, timezone
from gold_history import metadata, request, INST
from gold_research import ema
from gold_macro_probe import probe, SOURCES

def main():
    meta=metadata()
    macro=dict(probe((k,SOURCES[k])) for k in ("dxy","gold_futures_proxy","treasury_nominal","treasury_real"))
    rows=request("/market/history-candles",instId=INST,bar="1H",limit="300")
    now=int(time.time())
    rows=sorted([r for r in rows if r[8]=="1" and int(r[0])+3600000<=now*1000],key=lambda r:int(r[0]))
    out={"version":"hybrid-shadow-v2","instrument":INST,"captured_at":datetime.now(timezone.utc).isoformat(),"research_only":True,"orders_enabled":False,"signals_enabled":False,"macro":macro,"candidate":"NONE","blockers":[],"horizons_hours":[1,4,6,12,24]}
    b=out["blockers"]
    if len(rows)<200 or any(int(y[0])-int(x[0])!=3600000 for x,y in zip(rows,rows[1:])):
        b.append("insufficient_or_gapped_hourly_history")
    else:
        end=int(rows[-1][0])//1000+3600
        c=[float(r[4]) for r in rows]
        out.update(reference_close=c[-1],reference_close_ts=end)
        if now-end>3900 or float(rows[-1][5])<=0: b.append("stale_or_empty_okx_bar")
        e20,e50,e200=[ema(c,n) for n in (20,50,200)]
        direction=1 if e20[-1]>e50[-1]>e200[-1] else -1 if e20[-1]<e50[-1]<e200[-1] else 0
        out["technical_direction"]=direction
        for k in ("dxy","gold_futures_proxy"):
            m=macro[k]
            obs=m.get("closed_observations",[])
            if not m.get("validated") or len(obs)<2 or obs[-1][0]+3600!=end:
                b.append(k+"_not_aligned")
        if not b:
            d=macro["dxy"]["hourly_change_pct"]
            g=macro["gold_futures_proxy"]["hourly_change_pct"]
            if direction and direction*d<0 and direction*g>0:
                out["candidate"]="LONG" if direction==1 else "SHORT"
            else: b.append("no_intermarket_convergence")
    # Independently measure execution-feed freshness; never enable execution.
    checks={}
    for bar,step,limit in (("1m",60,90),("5m",300,360)):
        try:
            data=request("/market/candles",instId=INST,bar=bar,limit="10")
            t=int(time.time())
            closed=[r for r in data if r[8]=="1" and int(r[0])/1000+step<=t]
            last=max(closed,key=lambda r:int(r[0]))
            age=t-int(last[0])/1000-step
            checks[bar]={"age_seconds":age,"close":float(last[4]),"ok":0<=age<=limit and float(last[5])>0}
        except Exception as exc:
            checks[bar]={"ok":False,"error":str(exc)}
    for key,path in (("book","/market/books"),("trades","/market/trades")):
        try:
            data=request(path,instId=INST,limit="5")
            age=time.time()-max(int(r["ts"]) for r in data)/1000
            checks[key]={"age_seconds":age,"ok":0<=age<=180}
        except Exception as exc:
            checks[key]={"ok":False,"error":str(exc)}
    out["execution_feed"]=checks
    out["execution_feed_ready"]=all(v["ok"] for v in checks.values())
    out["calendar_status"]="NOT_VALIDATED"
    out["treasury_role"]="daily_context_only"
    out["execution_status"]="BLOCKED_RESEARCH_ONLY"
    Path("gold-hybrid.json").write_text(json.dumps(out,indent=2,allow_nan=False))

if __name__=="__main__":
    main()
