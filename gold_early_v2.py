"""Prospective microstructure research; sampled REST is not a complete trade tape."""
import math
from gold_early import evaluate

def update(state,book,trades,now):
    tape=state.setdefault("trades",{})
    for t in trades:
        ts=int(t["ts"])/1000; size=float(t["sz"])
        if 0<=now-ts<=300 and math.isfinite(size) and size>0 and t["side"] in ("buy","sell"):
            tape[str(t["tradeId"])]=dict(t)
    state["trades"]={k:t for k,t in tape.items() if 0<=now-int(t["ts"])/1000<=300}
    samples=state.setdefault("books",[])
    ts=int(book["ts"])/1000
    bid=sum(float(x[1]) for x in book["bids"][:5]); ask=sum(float(x[1]) for x in book["asks"][:5])
    if 0<=now-ts<=30 and min(bid,ask)>0 and math.isfinite(bid+ask) and not any(s["ts"]==ts for s in samples):
        samples.append({"ts":ts,"imbalance":(bid-ask)/(bid+ask)})
    state["books"]=sorted([s for s in samples if 0<=now-s["ts"]<=300],key=lambda s:s["ts"])[-300:]
    return state

def assess(rows,state,book,now):
    trades=list(state.get("trades",{}).values())
    v1=evaluate(rows,now,book,trades)
    out={"version":"early-shadow-v2","research_only":True,"candidate":"NONE","v1":v1,"sampled_flow":{},"blockers":[]}
    for seconds in (60,180,300):
        subset=[t for t in trades if 0<=now-int(t["ts"])/1000<=seconds]
        buy=sum(float(t["sz"]) for t in subset if t["side"]=="buy")
        sell=sum(float(t["sz"]) for t in subset if t["side"]=="sell")
        out["sampled_flow"][str(seconds)]={"delta_contracts":buy-sell,"count":len(subset),"imbalance":(buy-sell)/(buy+sell) if buy+sell else None,"complete_tape":False}
    samples=[s for s in state.get("books",[]) if 0<=now-s["ts"]<=60]
    out["book_samples"]=len(samples)
    d={"LONG":1,"SHORT":-1}.get(v1["candidate"],0)
    if not d: out["blockers"].append("no_v1_candidate")
    if len(samples)<3 or (samples and samples[-1]["ts"]-samples[0]["ts"]<20): out["blockers"].append("insufficient_book_persistence")
    elif d and sum(d*s["imbalance"]>=.15 for s in samples)/len(samples)<.75: out["blockers"].append("book_not_persistent")
    for seconds,minimum in ((60,10),(180,20),(300,30)):
        f=out["sampled_flow"][str(seconds)]
        if f["count"]<minimum or f["imbalance"] is None:
            out["blockers"].append("insufficient_flow_sample_"+str(seconds))
        elif d and d*f["imbalance"]<.2:
            out["blockers"].append("flow_not_confirmed_"+str(seconds))
    if not out["blockers"]: out["candidate"]=v1["candidate"]
    return out
