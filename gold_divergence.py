"""Experimental divergence candidate, not proof of absorption or an executable signal."""
import math

def evaluate(observation):
    out={"version":"divergence-shadow-v1","research_only":True,"candidate":"NONE","blockers":[]}
    v=observation.get("v1",{})
    f=observation.get("sampled_flow",{})
    if not all(k in v for k in ("volume_ratio","momentum_3m","book_imbalance","book_age_seconds")):
        out["blockers"].append("upstream_microstructure_unavailable")
        return out
    try:
        ratio=float(v["volume_ratio"]); momentum=float(v["momentum_3m"]); book=float(v["book_imbalance"])
        age=float(v["book_age_seconds"])
        if not all(map(math.isfinite,(ratio,momentum,book,age))) or not 0<=age<=30 or abs(book)>1:
            raise ValueError("invalid or stale input")
        d=1 if momentum>0 else -1 if momentum<0 else 0
        if ratio<2 or not d or abs(momentum)<.0005: out["blockers"].append("weak_acceleration")
        if d*book>-.5: out["blockers"].append("no_opposing_book")
        for window,n in (("60",10),("180",20),("300",30)):
            x=f[window]; imbalance=float(x["imbalance"])
            if not math.isfinite(imbalance) or abs(imbalance)>1 or x["count"]<n or d*imbalance<.6:
                out["blockers"].append("flow_not_confirmed_"+window)
        if not out["blockers"]: out["candidate"]="LONG" if d==1 else "SHORT"
    except (KeyError,ValueError,TypeError) as exc:
        out["blockers"].append(str(exc))
    return out
