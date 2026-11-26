"""Technical paper-test hypothesis; not an executable order."""
import math
from gold_plan import plan
from gold_history import INST

def assess(market,quote,now):
    out={"version":"technical-shadow-v1","candidate":"NONE","status":"DATA_BLOCKED","research_only":True,"orders_enabled":False,"blockers":[],"observed_at":now}
    try:
        if market.get("instrument")!=INST: raise ValueError("instrument_unverified")
        series=market["series"]; trends={}
        for tf,step,grace in (("15m",900,120),("1H",3600,1800),("4H",14400,3600)):
            s=series[tf]; i=s["indicators"]
            if not 0<=now-s["bar_end"]<=step+grace: raise ValueError("stale_"+tf)
            a,b=i["ema20"],i["ema50"]
            if not all(math.isfinite(x) and x>0 for x in (a,b)): raise ValueError("invalid_ema")
            trends[tf]=1 if a>b else -1 if a<b else 0
        out["trends"]=trends
        d=trends["1H"]
        out["candidate"]="LONG" if d==1 else "SHORT" if d==-1 else "NONE"
        out["status"]="WATCH"
        if not d: return out
        s=series["15m"]; i=s["indicators"]
        if trends["15m"]!=d: out["blockers"].append("15m_countertrend")
        if trends["4H"]!=d: out["blockers"].append("4h_countertrend")
        rsi=i["rsi14"]; macd=i["macd"]; signal=i["macd_signal"]; atr=i["atr14"]
        if not all(math.isfinite(x) for x in (rsi,macd,signal,atr)) or atr<=0: raise ValueError("invalid_indicators")
        if not (35<=rsi<=70 if d==1 else 30<=rsi<=65): out["blockers"].append("rsi_extended")
        if d*(macd-signal)<=0: out["blockers"].append("macd_not_confirmed")
        if not quote.get("connected") or not 0<=now-float(quote["observed_at"])<=5: raise ValueError("quote_unavailable")
        b=quote["book"]
        if not 0<=now-int(b["ts"])/1000<=5: raise ValueError("quote_stale")
        bid=float(b["bids"][0][0]); ask=float(b["asks"][0][0])
        if not all(math.isfinite(x) and x>0 for x in (bid,ask)) or ask<bid: raise ValueError("invalid_quote")
        mid=(bid+ask)/2
        if ask-bid>.15*atr: out["blockers"].append("spread_large_relative_to_atr")
        p=plan(out["candidate"],mid,atr)
        if p["status"]!="SIMULATION_ONLY": raise ValueError("invalid_plan")
        out["reference_mid"]=mid
        out["scenario"]=p
        out["status"]="PAPER_TEST_CANDIDATE" if not out["blockers"] else "CONDITIONAL_WATCH"
        out["macro_required"]=False
        out["probability"]=None
    except (KeyError,ValueError,TypeError,IndexError,ZeroDivisionError) as e:
        out["blockers"].append(str(e))
        out["status"]="DATA_BLOCKED"
    return out
