"""Research transport, isolated from REST decisions until validated."""
import asyncio,json,time,logging
from collections import deque
from gold_history import INST
from gold_quotes import latest_trade, valid_book, valid_trade
import websockets

URL="wss://wseea.okx.com:8443/ws/v5/public"

class Feed:
    def __init__(self):
        self.reset()
    def reset(self):
        self.connected=False
        self.acks=set()
        self.book=None
        self.trades={}
        self.samples=deque(maxlen=300)
        self.connected_at=None
        self.last_message=None
    def ingest(self,msg,now):
        arg=msg.get("arg",{})
        if arg.get("instId")!=INST: return
        channel=arg.get("channel")
        if channel not in ("books5","trades"): return
        if msg.get("event")=="subscribe":
            self.acks.add(channel)
        for row in msg.get("data",[]):
            ts=int(row["ts"])/1000
            if not 0<=now-ts<=180: continue
            if channel=="books5":
                if not valid_book(row, now, 30): continue
                if self.book and int(row["ts"])<int(self.book["ts"]): continue
                self.book=dict(row)
            else:
                row = dict(row, instId=INST)
                if not valid_trade(row, now, 180) or row.get("side") not in ("buy","sell"): continue
                self.trades[str(row["tradeId"])]=dict(row)
        self.last_message=now
        self.prune(now)
    def prune(self,now):
        self.trades={k:v for k,v in self.trades.items() if 0<=now-int(v["ts"])/1000<=300}
        if len(self.trades)>20000:
            raise RuntimeError("trade buffer capacity exceeded")
    def status(self,now):
        self.prune(now)
        book_age=now-int(self.book["ts"])/1000 if self.book else None
        trade_age=now-max(int(v["ts"])/1000 for v in self.trades.values()) if self.trades else None
        flow={}
        for window in (60,180,300):
            rows=[v for v in self.trades.values() if 0<=now-int(v["ts"])/1000<=window]
            buy=sum(float(v["sz"]) for v in rows if v["side"]=="buy")
            sell=sum(float(v["sz"]) for v in rows if v["side"]=="sell")
            flow[str(window)]={"count":len(rows),"delta_contracts":buy-sell,"imbalance":(buy-sell)/(buy+sell) if buy+sell else None,"window_observed":self.connected_at is not None and now-self.connected_at>=window,"complete_tape":False}
        return {"flow":flow,"book":self.book,"connected":self.connected,"acks":sorted(self.acks),"book_age_seconds":book_age,"trade_age_seconds":trade_age,"trade_count_5m":len(self.trades),"research_only":True,"complete_tape":False,"transport_ready":self.connected and self.acks=={"books5","trades"} and book_age is not None and 0<=book_age<=30 and trade_age is not None and 0<=trade_age<=180}
    def live(self, now):
        self.prune(now)
        latest = latest_trade(self.trades.values(), now)
        start = int(now) // 60 * 60
        trades = sorted((t for t in self.trades.values() if int(t["ts"]) >= start*1000),
                        key=lambda t: (int(t["ts"]), str(t["tradeId"]).zfill(32)))
        prices = [float(t["px"]) for t in trades]
        forming = [start, prices[0], max(prices), min(prices), prices[-1],
                   sum(float(t["sz"]) for t in trades)] if prices else None
        return dict(observed_at=now, connected=self.connected, book=self.book, last_trade=latest,
                    flow=self.status(now)["flow"], forming_1m=forming)

async def run(feed,on_status=None,on_live=None):
    delay=1
    while True:
        feed.reset()
        try:
            async with websockets.connect(URL,open_timeout=10,close_timeout=3,max_size=1048576) as ws:
                feed.connected=True
                feed.connected_at=time.time()
                await ws.send(json.dumps({"op":"subscribe","args":[{"channel":c,"instId":INST} for c in ("books5","trades")]}))
                last_status=0
                last_live=0
                last_received = time.monotonic()
                ping_sent = False
                while True:
                    raw = None
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1)
                    except asyncio.TimeoutError:
                        silence = time.monotonic() - last_received
                        if silence > 25:
                            raise TimeoutError("OKX WS heartbeat timed out")
                        if silence > 15 and not ping_sent:
                            await ws.send("ping")
                            ping_sent = True
                    now=time.time()
                    if raw is not None:
                        last_received = time.monotonic()
                        ping_sent = False
                    if raw not in (None, "pong"):
                        msg=json.loads(raw)
                        if msg.get("event")=="error": raise RuntimeError(str(msg))
                        feed.ingest(msg,now)
                    if now-feed.connected_at>20 and len(feed.acks)!=2: raise RuntimeError("missing subscription acknowledgement")
                    if on_live and now-last_live>=1:
                        on_live(feed.live(now))
                        last_live=now
                    if now-last_status>=15:
                        status=feed.status(now)
                        if on_status: on_status(status)
                        last_status=now
                    if now-feed.connected_at>60: delay=1
        except asyncio.CancelledError: raise
        except Exception:
            logging.exception("gold websocket disconnected")
        finally:
            feed.reset()
            if on_status: on_status(feed.status(time.time()))
            if on_live: on_live({"observed_at":time.time(),"connected":False,"book":None,"last_trade":None,"flow":{}})
        await asyncio.sleep(delay)
        delay=min(60,delay*2)
