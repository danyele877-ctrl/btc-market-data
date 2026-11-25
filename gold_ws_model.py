"""Experimental WS model; no complete tape or execution claim."""
import math
def assess(rest,ws,now):
    out={"version":"ws-shadow-v1","candidate":"NONE","research_only":True,"blockers":[]}
    try:
        if not ws or not ws.get("transport_ready"): raise ValueError("ws_not_ready")
        age=now-float(ws["observed_at"])
        if not 0<=age<=30: raise ValueError("ws_snapshot_stale")
        book=ws["book"]
        if not 0<=now-int(book["ts"])/1000<=30: raise ValueError("book_stale")
        if not 0<=age+float(ws["trade_age_seconds"])<=180: raise ValueError("trades_stale")
        v=rest["v1"]
        if not 0<=now-float(v["bar_close_ts"])<=90: raise ValueError("candle_stale")
        ratio=float(v["volume_ratio"]); momentum=float(v["momentum_3m"])
        bid=sum(float(x[1]) for x in book["bids"][:5]); ask=sum(float(x[1]) for x in book["asks"][:5])
        if min(bid,ask)<=0 or not all(map(math.isfinite,(bid,ask,ratio,momentum))): raise ValueError("invalid_metrics")
        imbalance=(bid-ask)/(bid+ask)
        d=1 if momentum>0 else -1 if momentum<0 else 0
        out.update(volume_ratio=ratio,momentum_3m=momentum,book_imbalance=imbalance)
        if ratio<1.5 or not d or d*imbalance<.15: raise ValueError("price_book_not_aligned")
        for key,minimum in (("60",10),("180",20),("300",30)):
            f=ws["flow"][key]
            if not f.get("window_observed") or f["count"]<minimum: raise ValueError("insufficient_ws_sample_"+key)
            if f["imbalance"] is None or not math.isfinite(f["imbalance"]) or d*f["imbalance"]<.2: raise ValueError("ws_flow_not_aligned_"+key)
        out["candidate"]="LONG" if d==1 else "SHORT"
    except (ValueError,KeyError,TypeError,IndexError,ZeroDivisionError) as e:
        out["blockers"].append(str(e))
    return out
