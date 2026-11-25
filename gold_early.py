"""Experimental early signal; never an executable recommendation."""
import math

def evaluate(rows, now, book=None, trades=None):
    out={"research_only":True,"candidate":"NONE","blockers":[],"orders_enabled":False}
    try:
        bars=sorted([r for r in rows if r[8]=="1" and int(r[0])/1000+60<=now],key=lambda r:int(r[0]))
        bars=bars[-31:]
        if len(bars)<31 or any(int(b[0])-int(a[0])!=60000 for a,b in zip(bars,bars[1:])):
            raise ValueError("insufficient contiguous closed minutes")
        age=now-int(bars[-1][0])/1000-60
        if not 0<=age<=90: raise ValueError("stale minute")
        c=[float(r[4]) for r in bars]; v=[float(r[5]) for r in bars]
        if not all(math.isfinite(x) and x>0 for x in c) or not all(math.isfinite(x) and x>=0 for x in v): raise ValueError("invalid values")
        base=sum(v[:-3])/28
        if base<=0 or min(v[-3:])<=0: raise ValueError("insufficient traded volume")
        ratio=sum(v[-3:])/3/base
        momentum=c[-1]/c[-4]-1
        out.update(volume_ratio=ratio,momentum_3m=momentum,bar_close_ts=int(bars[-1][0])/1000+60)
        if not book or not trades: raise ValueError("missing microstructure")
        age=now-int(book["ts"])/1000
        if not 0<=age<=180: raise ValueError("stale book")
        bid=sum(float(x[1]) for x in book["bids"][:5]); ask=sum(float(x[1]) for x in book["asks"][:5])
        if min(bid,ask)<=0 or not math.isfinite(bid+ask): raise ValueError("invalid book")
        imbalance=(bid-ask)/(bid+ask)
        recent={str(t["tradeId"]):t for t in trades if 0<=now-int(t["ts"])/1000<=60}
        if len(recent)<5: raise ValueError("insufficient recent trades")
        buy=sum(float(t["sz"]) for t in recent.values() if t["side"]=="buy")
        sell=sum(float(t["sz"]) for t in recent.values() if t["side"]=="sell")
        if min(buy,sell)<0 or not math.isfinite(buy+sell) or buy+sell<=0: raise ValueError("invalid trade volume")
        flow=(buy-sell)/(buy+sell)
        out.update(book_imbalance=imbalance,trade_imbalance=flow,trade_count=len(recent),book_age_seconds=age)
        if ratio>=1.5 and momentum>0 and imbalance>=.15 and flow>=.2: out["candidate"]="LONG"
        elif ratio>=1.5 and momentum<0 and imbalance<=-.15 and flow<=-.2: out["candidate"]="SHORT"
        else: out["blockers"].append("no_convergence")
    except (ValueError,KeyError,TypeError,IndexError,ZeroDivisionError) as e:
        out["blockers"].append(str(e))
    return out
