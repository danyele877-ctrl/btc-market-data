"""Read-only local dashboard snapshot; no public listener or notification delivery."""
import json,time
from pathlib import Path
from gold_tracking import save
from gold_swing import assess as swing_assess
from gold_technical import assess as technical_assess
from gold_paper import update as paper_update

def publish(root):
    root=Path(root); now=time.time()
    def load(path):
        try:
            data=json.loads(path.read_text())
            age=now-path.stat().st_mtime
            return {"age_seconds":age,"fresh":0<=age<=limits[path.name],"data":data}
        except (OSError,ValueError): return {"fresh":False,"data":None}
    limits={"latest_hybrid.json":420,"summary.json":120,"calendar_view.json":420,"ws_status.json":45,"market_view.json":180}
    hybrid=load(root/"latest_hybrid.json")
    early=load(root/"early"/"summary.json")
    calendar=load(root/"calendar_view.json")
    ws=load(root/"ws_status.json")
    if not ws["fresh"] and ws["data"]:
        ws["data"]["transport_ready"]=False
    market=load(root/"market_view.json")
    if hybrid["data"]:
        p=hybrid["data"].get("plan",{})
        if not hybrid["fresh"] or p.get("expires_at",0)<=now:
            hybrid["data"]["plan"]={"status":"EXPIRED_OR_BLOCKED","research_only":True}
    swing=swing_assess(market["data"] or {},now)
    try:
        quote=json.loads((root/"live_quote.json").read_text())
    except (OSError,ValueError):
        quote={}
    technical=technical_assess(market["data"] or {},quote,now)
    paper=paper_update(root,technical,swing,market["data"] or {},now)
    result={"paper":paper,"technical":technical,"swing":swing,"generated_at":now,"research_only":True,"orders_enabled":False,"notifications_enabled":False,"hybrid":hybrid,"early":early,"calendar":calendar,"websocket":ws,"market_view":market}
    save(root/"dashboard.json",result)
    return result
