"""Closed-candle GOLD research hypotheses shared by the live worker and replay."""
from math import floor, ceil, isfinite
from statistics import median

from gold_history import BARS, INST
from gold_research import ema

FRAMES = ("1m", "5m", "15m", "1H", "4H")
SPECS = {
    "TECHNICAL_V2_SCALP": ("SCALPING", "5m", "1m", 900, 2700),
    "TECHNICAL_V2_INTRADAY": ("INTRADAY", "15m", "1m", 1800, 21600),
    "REVERSAL": ("SCALPING", "5m", "1m", 900, 2700),
    "BREAKOUT": ("SCALPING", "5m", "1m", 900, 2700),
    "PULLBACK": ("INTRADAY", "15m", "1m", 1800, 21600),
    "SWING_V2": ("SWING", "1H", "5m", 21600, 259200),
}


def sign(value, threshold=0):
    return int(value > threshold) - int(value < -threshold)


def features(bars, tf, bar_end):
    step = BARS[tf] // 1000
    bars = bars[-220:]
    if len(bars) < 50 or bars[-1][0] + step != bar_end:
        return None
    for i in range(len(bars) - 1, 0, -1):
        if bars[i][0] - bars[i - 1][0] != step:
            bars = bars[i:]
            break
    if len(bars) < 50:
        return None
    close = [r[4] for r in bars]
    last = bars[-1]
    tr = [max(b[2] - b[3], abs(b[2] - a[4]), abs(b[3] - a[4]))
          for a, b in zip(bars[-29:-1], bars[-28:])]
    atr = sum(tr[-14:]) / 14
    vols = [r[5] for r in bars[-23:]]
    if atr <= 0 or sum(vols[-3:]) <= 0 or sum(vols[:-3]) <= 0:
        return None
    fast, slow = ema(close, 20), ema(close, 50)
    long = ema(close, 200) if len(close) >= 200 else None
    macd = [a - b for a, b in zip(ema(close, 12), ema(close, 26))]
    hist = macd[-1] - ema(macd, 9)[-1]
    changes = [b - a for a, b in zip(close[-15:-1], close[-14:])]
    gains = sum(max(0, x) for x in changes)
    losses = sum(max(0, -x) for x in changes)
    vwap_bars = bars[-20:]
    vwap = sum((r[2] + r[3] + r[4]) / 3 * r[5] for r in vwap_bars) / sum(vols[-20:])
    move = (close[-1] - close[-4]) / atr
    acceleration = move - (close[-4] - close[-7]) / atr
    a, b = bars[-11:-6], bars[-6:-1]
    structure = (sign(max(r[2] for r in b) - max(r[2] for r in a), .1 * atr)
                 + sign(min(r[3] for r in b) - min(r[3] for r in a), .1 * atr))
    trend = sum((sign(fast[-1] - slow[-1], .2 * atr), sign(fast[-1] - fast[-4], .1 * atr),
                 sign(slow[-1] - slow[-4], .05 * atr), structure,
                 sign(slow[-1] - long[-1], .3 * atr) + sign(long[-1] - long[-4], .02 * atr) if long else 0))
    momentum = sum((sign(move, .45), sign(hist, .08 * atr),
                    sign(last[4] - vwap, .2 * atr), sign(acceleration, .45)))
    prev = bars[-21:-1]
    return dict(end=bar_end, close=last[4], bar=last, prev=bars[-2], atr=atr,
                atr_ratio=atr / (sum(tr[:14]) / 14) if sum(tr[:14]) else 1,
                rsi=100 * gains / (gains + losses) if gains + losses else 50,
                ema=fast[-1], ema50=slow[-1], ema200=long[-1] if long else None,
                vwap=vwap, trend=trend, momentum=momentum, move=move,
                acceleration=acceleration, volume=sum(vols[-3:]) / 3 / (sum(vols[:-3]) / 20),
                resistance=max(r[2] for r in prev), support=min(r[3] for r in prev),
                recent_low=min(r[3] for r in bars[-5:]), recent_high=max(r[2] for r in bars[-5:]))


def quote_book(quote, now):
    try:
        book = quote["book"]
        bid, ask = float(book["bids"][0][0]), float(book["asks"][0][0])
        if (quote.get("connected") and quote.get("book_source", "OKX_EEA_WS") == "OKX_EEA_WS"
                and 0 <= now - float(quote["observed_at"]) <= 5 and 0 <= now - int(book["ts"]) / 1000 <= 5
                and 0 < bid <= ask and all(map(isfinite, (bid, ask)))):
            return bid, ask
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    return None


def limit_plan(d, frame, slower, level, trigger, validity, swing):
    atr, price = frame["atr"], frame["close"]
    rounding = floor if d > 0 else ceil
    tick = lambda value: round(rounding(value * 10) / 10, 1)
    entry = tick(price - d * .25 * atr if level is None
                 else min(level, price - .10 * atr) if d > 0 else max(level, price + .10 * atr))
    band = frame["recent_low"] if d > 0 else frame["recent_high"]
    distance = max(1.25 * atr, min(2.5 * atr, d * (entry - band) + .15 * atr))
    stop = tick(entry - d * distance)
    risk = abs(entry - stop)
    if not risk or d * (entry - stop) <= 0:
        return None
    nearby = sorted(d * (v - entry) / risk for v in (frame["support"], frame["resistance"],
                    slower["support"], slower["resistance"]) if d * (v - entry) > 0)
    targets = []
    for base in ((1.5, 2.5, 4) if swing else (1.25, 2, 3)):
        minimum = max(base, targets[-1] + .35 if targets else base)
        targets.append(next((v for v in nearby if minimum <= v <= minimum + .5), minimum))
    prices = [tick(entry + d * rr * risk) for rr in targets]
    if min(entry, stop, *prices) <= 0 or any(d * (b - a) <= 0 for a,b in zip([entry, *prices], prices)):
        return None
    plan = dict(entry_limit=entry, stop=stop, invalidation=stop, trigger=trigger,
                validity_seconds=validity, status="SIMULATION_ONLY")
    for i, value in enumerate(prices, 1):
        plan[f"tp{i}"] = value
        plan[f"rr{i}"] = abs(value - entry) / risk
    return plan


def evaluate(frames, now, quote=None, historical=False):
    out = {}
    fresh = {tf: f for tf, f in frames.items() if f and 0 <= now - f["end"] <= BARS[tf] / 1000 + 75}
    scenarios = dict.fromkeys(("5–15 min", "15–60 min", "1–6 ore", "6–24 ore", "24–72 ore"), "N/D")
    one, five, fifteen, hour, four = (fresh.get(tf) for tf in FRAMES)
    fast_score = one["momentum"] + 2 * five["momentum"] + fifteen["momentum"] if one and five and fifteen else None
    day_score = five["momentum"] + 2 * fifteen["momentum"] + hour["trend"] if five and fifteen and hour else None
    for label, score, threshold in (
        ("5–15 min", fast_score, 4), ("15–60 min", day_score, 4),
        ("1–6 ore", 2 * hour["trend"] + fifteen["momentum"] if hour and fifteen else None, 5),
        ("6–24 ore", hour["trend"] + 2 * four["trend"] if hour and four else None, 7),
        ("24–72 ore", 3 * four["trend"] + hour["trend"] if four and hour else None, 10),
    ):
        if score is not None:
            scenarios[label] = {1: "LONG", -1: "SHORT", 0: "LATERALE"}[sign(score, threshold)]
    book = quote_book(quote or {}, now)
    for model, (horizon, reference, evaluation, validity, holding) in SPECS.items():
        f = fresh.get(reference)
        signal = dict(candidate="NONE", status="DATA_BLOCKED", blockers=[], horizon=horizon,
                      evaluation_bar=evaluation, holding_seconds=holding, reference=f["end"] if f else None)
        out[model] = signal
        if model == "SWING_V2":
            if not hour or not four:
                signal["blockers"].append("stale_or_insufficient_1H_4H")
                continue
            slow = four
        else:
            if fast_score is None or day_score is None or not four:
                signal["blockers"].append("stale_or_insufficient_multitimeframe")
                continue
            slow = hour if reference != "15m" else four
        signal["status"] = "WATCH"
        d, level = 0, None
        trigger = ""
        if model.startswith("TECHNICAL_V2"):
            score = (fast_score + .25 * hour["trend"] + .15 * four["trend"] if reference == "5m"
                     else day_score + .25 * four["trend"])
            d = sign(score, 4)
            if d * f["momentum"] < 2 or d * f["move"] < .6 or f["volume"] < .55:
                d = 0
            trigger = "Momentum su candele chiuse e ingresso su ritracciamento"
        elif model == "REVERSAL":
            for side in (1, -1):
                swept = (f["bar"][3] < f["support"] < f["close"] if side == 1
                         else f["bar"][2] > f["resistance"] > f["close"])
                if swept and side * one["momentum"] >= 2 and side * f["acceleration"] >= .45 and f["volume"] >= 1:
                    d, level = side, f["support"] if side > 0 else f["resistance"]
                    break
            trigger = "Rientro dopo rottura fallita e accelerazione 1m"
        elif model == "BREAKOUT":
            for side in (1, -1):
                boundary = f["resistance"] if side > 0 else f["support"]
                if (side * (f["close"] - boundary) > .15 * f["atr"] and f["volume"] >= 1.25
                        and side * f["momentum"] >= 2 and side * one["momentum"] >= 0):
                    d, level = side, boundary
                    break
            trigger = "Chiusura 5m oltre il livello con volume e retest limit"
        elif model == "PULLBACK":
            side = sign(hour["trend"], 2)
            near = abs(f["prev"][4] - f["ema"]) <= .85 * f["atr"]
            if side and near and side * f["momentum"] >= 2 and side * five["momentum"] >= 1 and f["volume"] >= .6:
                d, level = side, f["ema"]
            trigger = "Ripresa del trend 1H dopo retest EMA20 su 15m"
        else:
            side = sign(four["trend"], 3)
            if side and side * hour["trend"] >= 2 and side * hour["momentum"] >= 2 and four["ema200"] is not None:
                d, level = side, hour["ema"]
                if abs(level - hour["close"]) > 2 * hour["atr"]:
                    d = 0
            trigger = "Struttura 4H e ripresa del momentum 1H su retest"
        if not d:
            signal["blockers"].append("no_closed_trigger")
            continue
        if d * (f["rsi"] - 50) > 33 or f["atr_ratio"] > 3:
            signal["blockers"].append("extended_or_dislocated_market")
            continue
        plan = limit_plan(d, f, slow, level, trigger, validity, horizon == "SWING")
        if not plan:
            signal["blockers"].append("invalid_levels")
            continue
        blockers = signal["blockers"]
        if not historical:
            if not book:
                blockers.append("ws_quote_unavailable")
            elif book[1] - book[0] > .15 * f["atr"]:
                blockers.append("spread_large_relative_to_atr")
            elif d * ((book[0] + book[1]) / 2 - plan["invalidation"]) <= 0:
                blockers.append("plan_already_invalidated")
                continue
        signal.update(candidate="LONG" if d > 0 else "SHORT",
                      status="CONDITIONAL_WATCH" if blockers else "PAPER_TEST_CANDIDATE",
                      plan=plan, reason=trigger)
    return dict(strategies=out, scenarios=scenarios)


def assess(market, quote, now):
    frames = {}
    if market.get("instrument") == INST:
        for tf in FRAMES:
            series = market.get("series", {}).get(tf)
            if series:
                frames[tf] = features(series["bars"], tf, series["bar_end"])
    return evaluate(frames, now, quote)
