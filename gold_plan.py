"""Hypothetical plans only; no order or sizing integration."""
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
import math

def plan(direction,price,atr,tick="0.10"):
    out={"research_only":True,"orders_enabled":False,"status":"BLOCKED"}
    if direction not in ("LONG","SHORT") or not all(math.isfinite(x) and x>0 for x in (price,atr)): return out
    t=Decimal(tick)
    if t<=0: return out
    def rounded(x,up):
        return float((Decimal(str(x))/t).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)*t)
    d=1 if direction=="LONG" else -1
    entry=rounded(price-d*.25*atr,d<0)
    stop=rounded(entry-d*1.5*atr,d<0)
    risk=abs(entry-stop)
    tp1=rounded(entry+d*1.5*risk,d<0)
    tp2=rounded(entry+d*2.5*risk,d<0)
    if min(entry,stop,tp1,tp2)<=0 or risk<=0 or d*(entry-stop)<=0 or d*(tp1-entry)<=0 or d*(tp2-tp1)<=0: return out
    return dict(out,status="SIMULATION_ONLY",direction=direction,entry_limit=entry,stop=stop,tp1=tp1,tp2=tp2,rr1=abs(tp1-entry)/risk,rr2=abs(tp2-entry)/risk,validity_seconds=300,fee_roundtrip_assumption=.001)
