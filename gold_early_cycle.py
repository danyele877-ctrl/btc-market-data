"""Single-writer early research cycle; separate from HYBRID state."""
import json,time,logging
from pathlib import Path
from gold_history import request,INST
from gold_tracking import save
from gold_early_v2 import update,assess
from gold_divergence import evaluate
from gold_early_outcomes import settle
from gold_dashboard import summarize,preview

def compare_transport(rest,ws,observed):
    """Paired diagnostics only; never promote a source from a single snapshot."""
    out={"status":"UNAVAILABLE","research_only":True,"windows":{}}
    if not isinstance(ws,dict): return out
    try:
        age=observed-float(ws["observed_at"])
        out["ws_snapshot_age_seconds"]=age
        if not 0<=age<=45 or not ws.get("connected"): return out
        out["status"]="CONNECTED" if not ws.get("transport_ready") else "READY"
        out["ws_book_age_seconds"]=ws.get("book_age_seconds")
        out["ws_trade_age_seconds"]=ws.get("trade_age_seconds")
        for key in ("60","180","300"):
            w=ws.get("flow",{}).get(key,{})
            r=rest.get("sampled_flow",{}).get(key,{})
            if not w or not r: continue
            wi=w.get("imbalance"); ri=r.get("imbalance")
            out["windows"][key]={"ws_count":w.get("count"),"rest_count":r.get("count"),
                "ws_delta_contracts":w.get("delta_contracts"),"rest_delta_contracts":r.get("delta_contracts"),
                "ws_window_observed":bool(w.get("window_observed")),
                "imbalance_difference":wi-ri if wi is not None and ri is not None else None,
                "complete_tape":False,"exactly_aligned":False}
    except (ValueError,TypeError,KeyError):
        out["status"]="INVALID"
    return out

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
    try:
        ws=json.loads((root.parent/"ws_status.json").read_text())
    except (OSError,ValueError):
        ws=None
    result["transport_comparison"]=compare_transport(result,ws,observed)
    path=root/(str(minute)+".json")
    if not path.exists(): save(path,result)
    state["last_minute"]=minute
    recent=[]
    for p in sorted(root.glob("[0-9]*.json"))[-120:]:
        x=json.loads(p.read_text())
        if observed-x["observed_at"]<=7200:
            outcomes=dict(x.get("outcomes",{}))
            for model,values in settle(x,rows,observed).items():
                outcomes[model]={**outcomes.get(model,{}),**values}
            if outcomes!=x.get("outcomes",{}):
                x["outcomes"]=outcomes
                save(p,x)
        recent.append(x)
    summary=summarize(recent)
    summary["notification_preview"]=preview(summary)
    summary["transport"]="REST_SAMPLED"
    save(root/"summary.json",summary)
    logging.info("EARLY collection complete: observations=%s V1=%s V2=%s divergence=%s",summary["observations"],result.get("v1",{}).get("candidate"),result.get("candidate"),result.get("divergence",{}).get("candidate"))
