"""Pure read-only presentation; no network, credentials or delivery."""
from collections import Counter
import math,time

def summarize(observations,now=None):
    now=time.time() if now is None else now
    models={"EARLY V1":lambda x:x.get("v1",{}),"EARLY V2":lambda x:x,"DIVERGENCE":lambda x:x.get("divergence",{}),"INTERMARKET":lambda x:x.get("intermarket",{}),"WS SHADOW":lambda x:x.get("ws_shadow",{})}
    result={"research_only":True,"orders_enabled":False,"observations":len(observations),"models":{}}
    for name,get in models.items():
        counts=Counter(); blockers=Counter()
        for x in observations:
            m=get(x); counts[m.get("candidate","NONE") if m else "NOT_EVALUATED"]+=1; blockers.update(m.get("blockers",[]))
        result["models"][name]={"counts":dict(counts),"blockers":dict(blockers)}
    keys={"EARLY V1":"early_v1","EARLY V2":"early_v2","DIVERGENCE":"divergence","INTERMARKET":"intermarket","WS SHADOW":"ws_shadow"}
    for name,get in models.items():
        stats={}
        for h in (5,15,30,45):
            for direction in ("LONG","SHORT"):
                eligible=[x for x in observations if get(x).get("candidate")==direction]
                values=[]; pending=0; missing=0; independent=[]; last_end=-1
                for x in sorted(eligible,key=lambda x:x["observed_at"]):
                    end=(int(x["observed_at"]//60)+1)*60+h*60
                    o=x.get("outcomes",{}).get(keys[name],{}).get(str(h))
                    if end>now: pending+=1; continue
                    if not o or not math.isfinite(o.get("net_fee_only",float("nan"))): missing+=1; continue
                    values.append(o["net_fee_only"])
                    if o["entry_ts"]>=last_end:
                        independent.append(o["net_fee_only"]); last_end=o["exit_ts"]
                stats[f"{direction}_{h}m"]={"signals":len(eligible),"pending":pending,"missing":missing,"settled":len(values),"positive_rate_fee_only":sum(v>0 for v in values)/len(values) if values else None,"mean_net_fee_only":sum(values)/len(values) if values else None,"nonoverlapping_n":len(independent),"nonoverlapping_mean_net_fee_only":sum(independent)/len(independent) if independent else None}
        result["models"][name]["outcomes"]=stats
    result["scope"]="rolling_last_120_observations"
    comparisons=[x.get("transport_comparison",{}) for x in observations]
    result["transport_coverage"]={"statuses":dict(Counter(c.get("status","UNAVAILABLE") for c in comparisons)),"windows":{}}
    for key in ("60","180","300"):
        pairs=[c["windows"][key] for c in comparisons if c.get("status")=="READY" and key in c.get("windows",{}) and c["windows"][key].get("ws_window_observed")]
        usable=[p for p in pairs if p.get("imbalance_difference") is not None]
        result["transport_coverage"]["windows"][key]={"paired":len(pairs),"mean_ws_count":sum(p["ws_count"] for p in pairs)/len(pairs) if pairs else None,"mean_rest_count":sum(p["rest_count"] for p in pairs)/len(pairs) if pairs else None,"mean_abs_imbalance_difference":sum(abs(p["imbalance_difference"]) for p in usable)/len(usable) if usable else None,"exactly_aligned":False}
    result["latest"]=observations[-1] if observations else None
    return result

def preview(snapshot):
    lines=["GOLD RADAR — SIMULAZIONE", "Nessun ordine automatico"]
    for name,m in snapshot.get("models",{}).items():
        c=m["counts"]
        lines.append(f"{name}: LONG {c.get('LONG',0)} | SHORT {c.get('SHORT',0)} | NONE {c.get('NONE',0)}")
    return "\n".join(lines)
