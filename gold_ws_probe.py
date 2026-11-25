"""Diagnostic only; no production feed replacement."""
import asyncio,json,time
from gold_history import INST,metadata
import websockets

async def probe():
    metadata()
    url="wss://wseea.okx.com:8443/ws/v5/public"
    out={"endpoint":url,"instrument":INST,"research_only":True,"channels":{},"errors":[]}
    try:
        async with websockets.connect(url,open_timeout=10,close_timeout=3) as ws:
            await ws.send(json.dumps({"op":"subscribe","args":[{"channel":c,"instId":INST} for c in ("books5","trades")]}))
            end=time.monotonic()+45
            while time.monotonic()<end:
                try: raw=await asyncio.wait_for(ws.recv(),timeout=min(10,max(.1,end-time.monotonic())))
                except asyncio.TimeoutError:
                    await ws.send("ping"); continue
                if raw=="pong": continue
                msg=json.loads(raw)
                if msg.get("event")=="error": out["errors"].append(msg); continue
                arg=msg.get("arg",{})
                if arg.get("instId")!=INST: continue
                c=arg.get("channel")
                if c not in ("books5","trades"): continue
                s=out["channels"].setdefault(c,{"ack":False,"messages":0,"records":0,"max_age_seconds":None})
                if msg.get("event")=="subscribe": s["ack"]=True
                if msg.get("data"):
                    s["messages"]+=1
                    for row in msg["data"]:
                        age=time.time()-int(row["ts"])/1000
                        s["records"]+=1
                        s["max_age_seconds"]=max(age,s["max_age_seconds"] if s["max_age_seconds"] is not None else age)
    except Exception as e: out["errors"].append(str(e))
    out["transport_observed"]=all(out["channels"].get(c,{}).get("ack") and out["channels"][c]["records"]>0 for c in ("books5","trades"))
    out["production_validated"]=False
    with open("gold-ws-probe.json","w") as f: json.dump(out,f,indent=2)

if __name__=="__main__": asyncio.run(probe())
