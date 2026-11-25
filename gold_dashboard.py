"""Pure read-only presentation; no network, credentials or delivery."""
from collections import Counter

def summarize(observations):
    models={"EARLY V1":lambda x:x.get("v1",{}),"EARLY V2":lambda x:x,"DIVERGENCE":lambda x:x.get("divergence",{}),"INTERMARKET":lambda x:x.get("intermarket",{})}
    result={"research_only":True,"orders_enabled":False,"observations":len(observations),"models":{}}
    for name,get in models.items():
        counts=Counter(); blockers=Counter()
        for x in observations:
            m=get(x); counts[m.get("candidate","NONE")]+=1; blockers.update(m.get("blockers",[]))
        result["models"][name]={"counts":dict(counts),"blockers":dict(blockers)}
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
