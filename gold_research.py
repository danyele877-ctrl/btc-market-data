"""Research only. Fixed rules, chronological holdout, no execution."""
import json, math, csv, io, urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
import argparse

def ema(values, n):
    out=[values[0]]
    for v in values[1:]:
        out.append(out[-1]+2/(n+1)*(v-out[-1]))
    return out

def evaluate(rows, horizon, fee=.001, breakout=False, pullback=False, regime=False):
    rows=sorted(rows,key=lambda r:int(r[0]))
    c=[float(r[4]) for r in rows]
    e20,e50,e200=(ema(c,n) for n in (20,50,200))
    tr=[float(rows[0][2])-float(rows[0][3])]
    for i in range(1,len(rows)):
        h,l=float(rows[i][2]),float(rows[i][3])
        tr.append(max(h-l,abs(h-c[i-1]),abs(l-c[i-1])))
    macd=[a-b for a,b in zip(ema(c,12),ema(c,26))]
    signal=ema(macd,9)
    split=max(200,int(len(rows)*.7))
    results={}
    for label,start,end in [('development',200,split),('holdout',split,len(rows))]:
        trades=[]; i=start
        while i+horizon<end:
            direction=1 if e20[i]>e50[i]>e200[i] else -1 if e20[i]<e50[i]<e200[i] else 0
            if regime:
                atr_now=sum(tr[i-13:i+1])/14
                trend=abs(e20[i]-e50[i])>=atr_now and atr_now>0
                expansion=atr_now>sum(tr[i-49:i+1])/50
                if not trend:
                    direction=0
                use_breakout=expansion
                use_pullback=not expansion
            else:
                use_breakout=breakout
                use_pullback=pullback
            if use_breakout and not ((direction == 1 and c[i] > max(float(z[2]) for z in rows[i-20:i])) or (direction == -1 and c[i] < min(float(z[3]) for z in rows[i-20:i]))):
                direction = 0
            if use_pullback:
                changes=[c[k]-c[k-1] for k in range(i-13,i+1)]
                gain=sum(max(0,v) for v in changes)
                loss=sum(max(0,-v) for v in changes)
                rsi=100*gain/(gain+loss) if gain+loss else 50
                atr_now=sum(tr[i-13:i+1])/14
                near=abs(c[i-1]-e20[i-1])<=atr_now
                long_ok=direction==1 and near and c[i]>e20[i] and c[i]>c[i-1] and 50<=rsi<=70 and macd[i]>signal[i]
                short_ok=direction==-1 and near and c[i]<e20[i] and c[i]<c[i-1] and 30<=rsi<=50 and macd[i]<signal[i]
                if not (long_ok or short_ok):
                    direction=0
            if not direction or float(rows[i][5])<=0:
                i+=1; continue
            entry=float(rows[i+1][1])
            atr=sum(tr[i-13:i+1])/14
            if atr<=0: i+=1; continue
            stop=entry-direction*1.5*atr; target=entry+direction*2.25*atr
            exit_price=c[i+horizon]; reason='timeout'
            for j in range(i+1,i+horizon+1):
                o,h,l=map(float,rows[j][1:4])
                if (l<=stop if direction==1 else h>=stop):
                    exit_price=min(o,stop) if direction==1 else max(o,stop); reason='stop'; break
                if (h>=target if direction==1 else l<=target):
                    exit_price=target; reason='target'; break
            net=direction*(exit_price-entry)/entry-fee
            trades.append((net,reason,direction))
            i+=horizon+1
        results[label]={'trades':len(trades),'long':sum(t[2]==1 for t in trades),'short':sum(t[2]==-1 for t in trades),'net_win_rate':sum(t[0]>0 for t in trades)/len(trades) if trades else None,'mean_net_return':sum(t[0] for t in trades)/len(trades) if trades else None,'target':sum(t[1]=='target' for t in trades),'stop':sum(t[1]=='stop' for t in trades),'timeout':sum(t[1]=='timeout' for t in trades)}
    return results

def macro():
    out={}
    for series in ('DTWEXBGS','DGS10','DFII10','FEDFUNDS','CPIAUCSL','PCEPI','PAYEMS'):
        try:
            url='https://fred.stlouisfed.org/graph/fredgraph.csv?id='+series+'&cosd='+(datetime.now(timezone.utc)-timedelta(days=180)).date().isoformat()
            req=urllib.request.Request(url,headers={'User-Agent':'GoldRadarDiagnostic/1.0'})
            with urllib.request.urlopen(req,timeout=30) as r:
                rows=list(csv.reader(io.StringIO(r.read().decode())))
            valid=[(d,float(v)) for d,v in rows[1:] if v not in ('','.')]
            out[series]={'latest':valid[-1],'previous':valid[-2],'retrieved_at':datetime.now(timezone.utc).isoformat(),'historical_backtest_allowed':False}
        except Exception as exc:
            out[series]={'error':str(exc)}
    return out

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('history');p.add_argument('--macro',action='store_true');a=p.parse_args()
    x=json.loads(Path(a.history).read_text())
    assert x['instrument']=='XAU-USD_UM_XPERP-310502'
    rows=x['series']['1H']['candles']
    assert all(r[8]=='1' for r in rows)
    assert all(int(b[0])-int(a[0])==3600000 for a,b in zip(rows,rows[1:]))
    report={'research_only':True,'round_trip_fee_assumption':.001,'spread_slippage_funding_included':False,'baseline':{h:evaluate(rows,h) for h in (1,4,6,12,24)},'breakout':{h:evaluate(rows,h,breakout=True) for h in (1,4,6,12,24)},'pullback':{h:evaluate(rows,h,pullback=True) for h in (1,4,6,12,24)},'regime':{h:{str(cost):evaluate(rows,h,fee=cost,regime=True) for cost in (.001,.0015,.002)} for h in (1,4,6,12,24)},'macro':macro() if a.macro else None}
    Path('gold-research.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))
