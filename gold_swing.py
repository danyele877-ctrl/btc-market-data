"""Swing research scenarios, not executable orders."""
import math
from decimal import Decimal,ROUND_FLOOR,ROUND_CEILING

def assess(market,now):
    out={"version":"swing-shadow-v1","research_only":True,"orders_enabled":False,"candidate":"NONE","plans":{},"blockers":[],"observed_at":now}
    try:
        series=market["series"]
        trends={}
        for tf,step,grace in (("1H",3600,1800),("4H",14400,3600),("1Dutc",86400,14400)):
            s=series[tf]
            if not 0<=now-s["bar_end"]<=step+grace: raise ValueError("stale_"+tf)
            i=s["indicators"]
            a,b=i["ema20"],i["ema50"]
            if not all(math.isfinite(v) and v>0 for v in (a,b)): raise ValueError("invalid_ema")
            trends[tf]=1 if a>b else -1 if a<b else 0
        out["trends"]=trends
        s=series["4H"]; i=s["indicators"]; atr=i["atr14"]; lo=i["support20"]; hi=i["resistance20"]
        p=series["1H"]["bars"][-1][4]
        if not all(math.isfinite(v) and v>0 for v in (atr,lo,hi,p)) or hi<=lo: raise ValueError("invalid_structure")
        out["reference_close"]=p
        out["atr4h"]=atr
        out["candidate"]="LONG" if all(v==1 for v in trends.values()) else "SHORT" if all(v==-1 for v in trends.values()) else "NONE"
        def tick(v,up):
            return float((Decimal(str(v))/Decimal(".1")).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)*Decimal(".1"))
        for direction,d in (("LONG",1),("SHORT",-1)):
            entry=tick(min(p-.25*atr,i["ema20"]) if d==1 else max(p+.25*atr,i["ema20"]),d==-1)
            stop=tick(min(lo-.25*atr,entry-1.5*atr) if d==1 else max(hi+.25*atr,entry+1.5*atr),d==-1)
            risk=abs(entry-stop)
            targets=[tick(entry+d*k*risk,d==-1) for k in (2,3,4)]
            if min(entry,stop,*targets)<=0 or risk<=0: continue
            out["plans"][direction]={"status":"CONDITIONAL_RESEARCH_ONLY","entry_limit":entry,"stop":stop,"tp1":targets[0],"tp2":targets[1],"tp3":targets[2],"rr1":abs(targets[0]-entry)/risk,"rr2":abs(targets[1]-entry)/risk,"rr3":abs(targets[2]-entry)/risk,"expires_at":now+21600,"trigger":"4H trend alignment and fresh quote; retest without structural invalidation","invalidation":stop,"preferred":out["candidate"]==direction,"funding_verified":False,"macro_verified":False}
    except (KeyError,ValueError,TypeError,IndexError,ZeroDivisionError) as e:
        out["blockers"].append(str(e))
        out["plans"]={}
        out["candidate"]="NONE"
    return out
