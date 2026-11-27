"""Research-only lead/lag hypothesis; no execution or probability claims."""
import math
from gold_macro_probe import aligned_hour
def assess(rows,macro,now):
    out={"version":"intermarket-shadow-v1","candidate":"NONE","research_only":True,"blockers":[],"observed_at":now}
    try:
        interval=aligned_hour(macro,now,max_age=3900)
        if not interval: raise ValueError("dxy_gc_no_fresh_common_hour")
        bars=sorted([r for r in rows if r[8]=="1" and int(r[0])/1000+60<=now],key=lambda r:int(r[0]))[-31:]
        if len(bars)!=31 or any(int(b[0])-int(a[0])!=60000 for a,b in zip(bars,bars[1:])): raise ValueError("minute_gap")
        if not 0<=now-int(bars[-1][0])/1000-60<=90: raise ValueError("stale_minute")
        c=[float(r[4]) for r in bars]; v=[float(r[5]) for r in bars]
        if not all(math.isfinite(x) and x>0 for x in c) or not all(math.isfinite(x) and x>=0 for x in v): raise ValueError("invalid_values")
        base=sum(v[:-3])/28
        if base<=0 or min(v[-3:])<=0: raise ValueError("insufficient_volume")
        ratio=sum(v[-3:])/3/base
        momentum=c[-1]/c[-4]-1
        d=interval["dxy_return"]; g=interval["gc_return"]
        out.update(dxy_return_1h=d,gc_return_1h=g,proxy_bar_end=interval["end"],momentum_3m=momentum,volume_ratio=ratio,bar_close_ts=int(bars[-1][0])/1000+60)
        if ratio>=1.5 and g>0 and d<0 and momentum>0: out["candidate"]="LONG"
        elif ratio>=1.5 and g<0 and d>0 and momentum<0: out["candidate"]="SHORT"
        else: out["blockers"].append("no_intermarket_convergence")
    except (KeyError,TypeError,ValueError,IndexError,ZeroDivisionError) as exc:
        out["blockers"].append(str(exc))
    return out
