"""Research display data, isolated from existing prediction thresholds."""
import math,time,logging
from pathlib import Path
from gold_history import request,INST,BARS
from gold_tracking import save
from gold_research import ema
CADENCE={"1m":60,"5m":300,"15m":900,"1H":1800,"4H":3600,"1Dutc":14400}

def analyze(rows,bar,now):
    step=BARS[bar]/1000
    records={}
    for r in rows:
        t=int(r[0])/1000
        if r[8]!="1" or t+step>now: continue
        a=[float(x) for x in r[1:6]]
        o,h,l,c,v=a
        if not all(map(math.isfinite,a)) or min(o,h,l,c)<=0 or h<max(o,c,l) or l>min(o,c,h) or v<0: raise ValueError("invalid candle")
        if t in records and records[t]!=a: raise ValueError("conflicting candle")
        records[t]=a
    bars=[[t,*records[t]] for t in sorted(records)]
    if not bars: raise ValueError("no closed bars")
    age=now-bars[-1][0]-step
    out={"bars":bars[-200:],"close_age_seconds":age,"bar_end":bars[-1][0]+step,"captured_at":now,"indicators":{}}
    tail=[bars[-1]]
    for b in reversed(bars[:-1]):
        if tail[-1][0]-b[0]!=step: break
        tail.append(b)
    tail.reverse()
    c=[b[4] for b in tail]
    ind=out["indicators"]
    for n in (20,50,200):
        if len(c)>=n: ind["ema"+str(n)]=ema(c,n)[-1]
    if len(c)>=15:
        tr=[max(b[2]-b[3],abs(b[2]-a[4]),abs(b[3]-a[4])) for a,b in zip(tail,tail[1:])]
        ind["atr14"]=sum(tr[-14:])/14
        changes=[b-a for a,b in zip(c[-15:-1],c[-14:])]
        gain=sum(max(x,0) for x in changes); loss=sum(max(-x,0) for x in changes)
        ind["rsi14"]=100*gain/(gain+loss) if gain+loss else 50
    if len(c)>=35:
        macd=[a-b for a,b in zip(ema(c,12),ema(c,26))]
        ind["macd"]=macd[-1]; ind["macd_signal"]=ema(macd,9)[-1]
    if len(tail)>=20:
        window=tail[-20:]; total=sum(b[5] for b in window)
        ind.update(support20=min(b[3] for b in window),resistance20=max(b[2] for b in window),vwap20=sum((b[2]+b[3]+b[4])/3*b[5] for b in window)/total if total else None)
    out["contiguous_bars"]=len(tail)
    return out

def worker(root):
    state={"instrument":INST,"series":{}}
    due={}
    while True:
        for bar,interval in CADENCE.items():
            if time.monotonic()<due.get(bar,0): continue
            try:
                rows=request("/market/history-candles",instId=INST,bar=bar,limit="300")
                state["series"][bar]=analyze(rows,bar,time.time())
            except Exception:
                logging.exception("chart refresh failed: %s",bar)
            due[bar]=time.monotonic()+interval
            save(Path(root)/"market_view.json",state)
        time.sleep(5)
