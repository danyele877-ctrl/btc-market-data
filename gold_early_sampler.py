"""Finite REST sampling experiment, independent of production worker."""
import json,time,logging
from pathlib import Path
from gold_history import request,metadata,INST
from gold_early_v2 import update,assess
from gold_tracking import save

def run(root,cycles=25,interval=15):
    if not 1<=cycles<=25 or interval<15: raise ValueError("sampling budget")
    root=Path(root); root.mkdir(parents=True,exist_ok=True)
    metadata()
    state={}; observations=[]; errors=[]; last_minute=None
    for i in range(cycles):
        started=time.monotonic()
        try:
            book=request("/market/books",instId=INST,limit="5")[0]
            trades=request("/market/trades",instId=INST,limit="100")
            now=time.time()
            update(state,book,trades,now)
            minute=int(now//60)
            if minute!=last_minute:
                rows=request("/market/candles",instId=INST,bar="1m",limit="100")
                observed=time.time()
                result=assess(rows,state,book,observed)
                observations.append(dict(result,observed_at=observed,instrument=INST))
                last_minute=minute
        except Exception as exc:
            errors.append({"observed_at":time.time(),"error":str(exc)})
        save(root/"early-sampling.json",{"research_only":True,"instrument":INST,"cycles_attempted":i+1,"observations":observations,"errors":errors,"transport":"REST_SAMPLED","orders_enabled":False})
        if i+1<cycles: time.sleep(max(0,interval-(time.monotonic()-started)))
    return observations

if __name__=="__main__":
    logging.basicConfig(level=logging.INFO)
    run("early-experiment")
