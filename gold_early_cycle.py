"""Single-writer early research cycle; separate from HYBRID state."""
import json,time
from pathlib import Path
from gold_history import request,INST
from gold_tracking import save
from gold_early_v2 import update,assess
from gold_divergence import evaluate
from gold_early_outcomes import settle
from gold_dashboard import summarize,preview

def cycle(root,state):
    root=Path(root)/"early"; root.mkdir(parents=True,exist_ok=True)
    book=request("/market/books",instId=INST,limit="5")[0]
    trades=request("/market/trades",instId=INST,limit="100")
    now=time.time()
    update(state,book,trades,now)
    minute=int(now//60)
    if state.get("last_minute")==minute: return
    rows=request("/market/candles",instId=INST,bar="1m",limit="100")
    observed=time.time()
    result=assess(rows,state,book,observed)
    result.update(divergence=evaluate(result),observed_at=observed,instrument=INST)
    path=root/(str(minute)+".json")
    if not path.exists(): save(path,result)
    state["last_minute"]=minute
    recent=[]
    for p in sorted(root.glob("[0-9]*.json"))[-120:]:
        x=json.loads(p.read_text())
        if observed-x["observed_at"]<=7200:
            outcomes=settle(x,rows,observed)
            if outcomes!=x.get("outcomes",{}):
                x["outcomes"]=outcomes
                save(p,x)
        recent.append(x)
    summary=summarize(recent)
    summary["notification_preview"]=preview(summary)
    summary["transport"]="REST_SAMPLED"
    save(root/"summary.json",summary)
