"""Read-only local dashboard snapshot; no public listener or notification delivery."""
import json,time
from pathlib import Path
from gold_tracking import save

def publish(root):
    root=Path(root); now=time.time()
    def load(path):
        try:
            data=json.loads(path.read_text())
            age=now-path.stat().st_mtime
            return {"age_seconds":age,"fresh":0<=age<=limits[path.name],"data":data}
        except (OSError,ValueError): return {"fresh":False,"data":None}
    limits={"latest_hybrid.json":420,"summary.json":120,"calendar_view.json":420}
    hybrid=load(root/"latest_hybrid.json")
    early=load(root/"early"/"summary.json")
    calendar=load(root/"calendar_view.json")
    if hybrid["data"]:
        p=hybrid["data"].get("plan",{})
        if not hybrid["fresh"] or p.get("expires_at",0)<=now:
            hybrid["data"]["plan"]={"status":"EXPIRED_OR_BLOCKED","research_only":True}
    result={"generated_at":now,"research_only":True,"orders_enabled":False,"notifications_enabled":False,"hybrid":hybrid,"early":early,"calendar":calendar}
    save(root/"dashboard.json",result)
    return result
