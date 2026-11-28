"""Research-only collection cycle. Requires a persistent directory and single writer."""
import json, time, argparse, os
from pathlib import Path
from datetime import datetime
from gold_hybrid import main as snapshot
from gold_history import collect
from gold_tracking import record, settle, save
from gold_plan import plan
from gold_macro_probe import macro_for_display
from gold_quotes import load

def run(root):
    root=Path(root); root.mkdir(parents=True,exist_ok=True)
    path=root/"ledger.json"
    ledger=json.loads(path.read_text()) if path.exists() else {}
    snapshot()
    s=json.loads(Path("gold-hybrid.json").read_text())
    now=time.time()
    diag=s.get("trigger_diagnostics",{})
    s["plan"]={"status":"BLOCKED","research_only":True}
    if s.get("trigger") in ("BREAKOUT","PULLBACK","BOTH") and s.get("execution_feed_ready") and not s.get("blockers") and not s.get("trigger_blockers") and 0<=now-diag.get("bar_close_ts",0)<=360:
        s["plan"]=plan(s["candidate"],diag["close"],diag["atr14"])
        s["plan"]["observed_at"]=now
        s["plan"]["expires_at"]=now+300
    previous = load(root/"latest_hybrid.json")
    s["macro_display"] = macro_for_display(s.get("macro", {}), previous.get("macro_display", previous.get("macro", {})))
    save(root/"latest_hybrid.json",s)
    # Persist observation before fetching outcomes; never reconstruct a missed signal.
    stamp=datetime.fromisoformat(s["captured_at"]).timestamp()
    key=str(int(stamp*1000))
    observations=root/"observations"
    observations.mkdir(exist_ok=True)
    save(observations/(key+".json"),s)
    record(ledger,s,now)
    save(path,ledger)
    if ledger:
        history=collect("5m",3)
        settle(ledger,history["candles"],time.time())
        save(path,ledger)
    summary={"research_only":True,"observations":len(list(observations.glob("*.json"))),"registered":len(ledger),"outcomes":{}}
    for h in (1,4,6,12,24):
        for direction in ("LONG","SHORT"):
            values=[v["outcomes"][str(h)]["net_fee_only"] for v in ledger.values() if v["direction"]==direction and str(h) in v["outcomes"]]
            summary["outcomes"][f"{direction}_{h}h"]={"n":len(values),"win_rate":sum(x>0 for x in values)/len(values) if values else None,"mean_net_fee_only":sum(values)/len(values) if values else None}
    save(root/"summary.json",summary)

if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--state",required=True);a=p.parse_args();run(a.state)
