"""Research-only lead/lag hypothesis; no execution or probability claims."""
import math
def assess(rows,macro,now):
    out={"version":"intermarket-shadow-v1","candidate":"NONE","research_only":True,"blockers":[],"observed_at":now}
    try:
        changes={}
        for name in ("dxy","gold_futures_proxy"):
            m=macro[name]
            if not m.get("validated"): raise ValueError(name+"_unvalidated")
            a,b=m["closed_observations"][-2:]
            end=int(b[0])+3600
            if b[0]-a[0]!=3600 or not 0<=now-end<=3900: raise ValueError(name+"_stale_or_gapped")
            if not all(math.isfinite(p) and p>0 for p in (a[1],b[1])): raise ValueError("invalid proxy price")
            changes[name]=(end,b[1]/a[1]-1)
        if changes["dxy"][0]!=changes["gold_futures_proxy"][0]: raise ValueError("proxy_time_mismatch")
        bars=sorted([r for r in rows if r[8]=="1" and int(r[0])/1000+60<=now],key=lambda r:int(r[0]))[-31:]
        if len(bars)!=31 or any(int(b[0])-int(a[0])!=60000 for a,b in zip(bars,bars[1:])): raise ValueError("minute_gap")
        if not 0<=now-int(bars[-1][0])/1000-60<=90: raise ValueError("stale_minute")
        c=[float(r[4]) for r in bars]; v=[float(r[5]) for r in bars]
        if not all(math.isfinite(x) and x>0 for x in c) or not all(math.isfinite(x) and x>=0 for x in v): raise ValueError("invalid_values")
        base=sum(v[:-3])/28
        if base<=0 or min(v[-3:])<=0: raise ValueError("insufficient_volume")
        ratio=sum(v[-3:])/3/base
        momentum=c[-1]/c[-4]-1
        d=changes["dxy"][1]; g=changes["gold_futures_proxy"][1]
        out.update(dxy_return_1h=d,gc_return_1h=g,proxy_bar_end=changes["dxy"][0],momentum_3m=momentum,volume_ratio=ratio,bar_close_ts=int(bars[-1][0])/1000+60)
        if ratio>=1.5 and g>0 and d<0 and momentum>0: out["candidate"]="LONG"
        elif ratio>=1.5 and g<0 and d>0 and momentum<0: out["candidate"]="SHORT"
        else: out["blockers"].append("no_intermarket_convergence")
    except (KeyError,TypeError,ValueError,IndexError,ZeroDivisionError) as exc:
        out["blockers"].append(str(exc))
    return out
