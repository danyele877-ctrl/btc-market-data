"""Research display data, isolated from existing prediction thresholds."""
import math,time,logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from gold_history import request,INST,BARS
from gold_tracking import save
from gold_research import ema

def analyze(rows,bar,now,history_limit=220):
    step=BARS[bar]/1000
    records={}
    forming=None
    for r in rows:
        t=int(r[0])/1000
        if r[8]=="0" and t<=now<t+step and len(r)>=9:
            provisional=[t,*map(float,r[1:6])]
            if all(map(math.isfinite,provisional)) and 0<provisional[3]<=min(provisional[1],provisional[4])<=max(provisional[1],provisional[4])<=provisional[2] and provisional[5]>=0:
                forming=provisional
        if r[8]!="1" or t+step>now: continue
        if t % step: raise ValueError("misaligned candle")
        a=[float(x) for x in r[1:6]]
        o,h,l,c,v=a
        if not all(map(math.isfinite,a)) or min(o,h,l,c)<=0 or h<max(o,c,l) or l>min(o,c,h) or v<0: raise ValueError("invalid candle")
        if t in records and records[t]!=a: raise ValueError("conflicting candle")
        records[t]=a
    bars=[[t,*records[t]] for t in sorted(records)]
    if not bars: raise ValueError("no closed bars")
    age=now-bars[-1][0]-step
    out={"bars":bars[-history_limit:] if history_limit else bars,"close_age_seconds":age,"bar_end":bars[-1][0]+step,"captured_at":now,"indicators":{}}
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
    out["forming_rest"]=forming
    return out

def worker(root):
    state = {"instrument": INST, "series": {}}
    due, pending = {}, {}
    def fetch(bar):
        rows = request("/market/candles", instId=INST, bar=bar, limit="300")
        return analyze(rows, bar, time.time())
    with ThreadPoolExecutor(max_workers=3) as pool:
        while True:
            now = time.time()
            for bar, job in list(pending.items()):
                if not job.done():
                    continue
                del pending[bar]
                step = BARS[bar] // 1000
                try:
                    series = job.result()
                    state["series"][bar] = series
                    due[bar] = (int(now) // step + 1) * step + 8 if now - series["bar_end"] < step else now + 12
                    save(Path(root) / "market_view.json", state)
                except Exception:
                    due[bar] = now + 30
                    logging.exception("chart refresh failed: %s", bar)
            for bar in BARS:
                if bar not in pending and now >= due.get(bar, 0):
                    pending[bar] = pool.submit(fetch, bar)
            time.sleep(2)
