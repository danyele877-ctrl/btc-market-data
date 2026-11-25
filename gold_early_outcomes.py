"""Research-only fee-adjusted terminal returns, not realized PnL."""
import math
HORIZONS=(5,15,30,45)
def settle(observation,rows,now,fee=.001):
    observed=float(observation["observed_at"])
    start=(int(observed//60)+1)*60
    bars={}
    for r in rows:
        ts=int(r[0])//1000
        if r[8]=="1" and ts+60<=now:
            if ts in bars and bars[ts]!=r: raise ValueError("conflicting candle")
            bars[ts]=r
    models={"early_v1":observation.get("v1",{}),"early_v2":observation,"divergence":observation.get("divergence",{})}
    result={}
    for name,model in models.items():
        direction=model.get("candidate")
        if direction not in ("LONG","SHORT"): continue
        outcomes={}
        for h in HORIZONS:
            if start+h*60>now: continue
            required=list(range(start,start+h*60,60))
            if any(t not in bars for t in required): continue
            entry=float(bars[start][1]); exit=float(bars[required[-1]][4])
            if not all(math.isfinite(x) and x>0 for x in (entry,exit)): continue
            gross=(exit/entry-1)*(1 if direction=="LONG" else -1)
            outcomes[str(h)]={"entry":entry,"exit":exit,"gross":gross,"net_fee_only":gross-fee,"entry_ts":start,"exit_ts":start+h*60}
        result[name]=outcomes
    return result
