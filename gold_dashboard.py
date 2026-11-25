"""Pure read-only presentation; no network, credentials or delivery."""
from collections import Counter

def summarize(observations):
    models={"EARLY V1":lambda x:x.get("v1",{}),"EARLY V2":lambda x:x,"DIVERGENCE":lambda x:x.get("divergence",{})}
    result={"research_only":True,"orders_enabled":False,"observations":len(observations),"models":{}}
    for name,get in models.items():
        counts=Counter(); blockers=Counter()
        for x in observations:
            m=get(x); counts[m.get("candidate","NONE")]+=1; blockers.update(m.get("blockers",[]))
        result["models"][name]={"counts":dict(counts),"blockers":dict(blockers)}
    result["latest"]=observations[-1] if observations else None
    return result

def preview(snapshot):
    lines=["GOLD RADAR — SIMULAZIONE", "Nessun ordine automatico"]
    for name,m in snapshot.get("models",{}).items():
        c=m["counts"]
        lines.append(f"{name}: LONG {c.get('LONG',0)} | SHORT {c.get('SHORT',0)} | NONE {c.get('NONE',0)}")
    return "\n".join(lines)
