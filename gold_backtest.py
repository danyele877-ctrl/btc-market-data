"""Chronological research replay for the exact OKX Europe XAU contract."""
import argparse
import json
import math
from bisect import bisect_left, bisect_right
from collections import Counter
from pathlib import Path

from gold_history import BARS, INST, metadata, request
from gold_market_view import analyze
from gold_paper import advance, valid_bar
from gold_strategies import FRAMES, SPECS, evaluate, features
from gold_technical import assess as technical_v1

FEES = (.0002, .0005)
SHORT_VALIDITY = (300, 900, 1800, 3600)
SWING_VALIDITY = (21600, 43200)
FUNDING_STEP = 28800


def fetch_history(directory, now, chart=FRAMES):
    import time
    directory.mkdir(parents=True, exist_ok=True)
    listed = int(metadata()["listTime"])
    for tf in chart:
        path = directory / f"gold-full-{tf}.ndjson"
        completed = path.with_suffix(".complete")
        if completed.exists():
            continue
        cursor = now * 1000
        if path.exists():
            with path.open() as previous:
                for line in previous:
                    cursor = min(int(row[0]) for row in json.loads(line))
        with path.open("a") as output:
            while True:
                page = request("/market/history-candles", instId=INST, bar=tf, limit="300", after=str(cursor))
                if not page:
                    break
                oldest = min(int(row[0]) for row in page)
                if oldest >= cursor:
                    raise ValueError(f"Pagination stalled for {tf} at {cursor}")
                output.write(json.dumps(page, separators=(",", ":")) + "\n")
                output.flush()
                cursor = oldest
                if cursor <= listed:
                    break
                time.sleep(.35)
        completed.write_text(str(cursor))


def load_history(path, tf, now):
    step = BARS[tf] // 1000
    records = {}
    with path.open() as file:
        for line in file:
            for row in json.loads(line):
                t, remainder = divmod(int(row[0]), 1000)
                if row[8] != "1" or t + step > now:
                    continue
                bar = (t, *map(float, row[1:6]))
                if remainder or not valid_bar(bar, step) or t in records and records[t] != bar:
                    raise ValueError(f"Malformed or conflicting {tf} candle at {row[0]}")
                records[t] = bar
    return sorted(records.values())


class Series:
    def __init__(self, rows, tf):
        self.rows = rows
        self.opens = [r[0] for r in rows]
        self.step = BARS[tf] // 1000
        self.tf = tf
        self.cache = (-1, None)

    def slice(self, start, end):
        return self.rows[bisect_left(self.opens, start):bisect_left(self.opens, end)]

    def current(self, now):
        i = bisect_right(self.opens, now - self.step)
        end = self.opens[i-1] + self.step if i else 0
        if i != self.cache[0]:
            self.cache = i, features(self.rows[max(0, i-220):i], self.tf, end)
        return self.cache[1]

    def legacy(self, now):
        i = bisect_right(self.opens, now - self.step)
        rows = [[str(b[0]*1000), *b[1:], 0, 0, "1"] for b in self.rows[max(0, i-300):i]]
        return analyze(rows, self.tf, now) if rows else {}


def simulate(series, signal, decision, validity):
    step = series.step
    start = (decision // step + 1) * step
    end = start + validity + signal["holding_seconds"]
    item = dict(direction=signal["candidate"], plan=signal["plan"], step=step, cursor=start,
                expires_at=start+validity, holding_seconds=signal["holding_seconds"], state="PENDING")
    bars = series.slice(start, end)
    advance(item, bars, end, historical_complete=True)
    return item


def return_after_fees(item):
    if item["state"] not in ("STOP", "FINAL_TARGET_TOUCHED", "TIMEOUT"):
        return None
    entry = item["plan"]["entry_limit"]
    exit_price = item.get("exit_price", item.get("last_close"))
    if exit_price is None:
        return None
    direction = 1 if item["direction"] == "LONG" else -1
    return direction * (exit_price - entry) / entry - FEES[0] - FEES[1] * exit_price / entry


def funding_adjustment(item, rates, series):
    if return_after_fees(item) is None or not rates:
        return None
    step = series.step
    first, last = item["filled_at"] - step, item["ended_at"]
    # A settlement inside an entry or terminal candle has unknown ordering.
    settlements = range((first // FUNDING_STEP + 1) * FUNDING_STEP, last + 1, FUNDING_STEP)
    if any(t <= item["filled_at"] or t > last - step for t in settlements):
        return None
    total = 0.
    sign = 1 if item["direction"] == "LONG" else -1
    for stamp in settlements:
        row = series.slice(stamp-step,stamp)
        if stamp not in rates or not row:
            return None
        close = row[0][4]  # Settlement mark unavailable; contract close is an explicit proxy.
        total -= sign * rates[stamp] * close / item["plan"]["entry_limit"]
    return total

def stats(items):
    states = Counter(item["state"] for item in items)
    entries = sum("filled_at" in item for item in items)
    targets = Counter(k for item in items for k in item.get("targets_touched", {}))
    values = [v for item in items if (v := return_after_fees(item)) is not None]
    high, equity, drawdown = 1., 1., 0.
    for value in values:
        equity *= 1 + value
        high = max(high, equity)
        drawdown = max(drawdown, 1 - equity / high)
    profit = sum(v for v in values if v > 0)
    loss = -sum(v for v in values if v < 0)
    eligible = len(items) - states["DATA_GAP"]
    with_funding = [return_after_fees(item) + item["funding_adjustment"] for item in items
                    if item.get("funding_adjustment") is not None]
    return dict(n=len(items), states=dict(states), n_crossed=entries,
                entry_rate=entries / eligible if eligible else None, targets=dict(targets),
                target_rate={k: targets[k] / entries if entries else None for k in ("tp1", "tp2", "tp3")},
                stop_without_tp1=sum(x["state"]=="STOP" and not x.get("targets_touched") for x in items),
                ambiguous_entry=sum(x.get("ambiguity","").startswith("entry_and") for x in items),
                profit_factor=profit / loss if loss else None, max_drawdown=drawdown if values else None,
                expectancy=sum(values)/len(values) if values else None, net_return=equity-1 if values else None,
                priced_n=len(values), funding_priced_n=len(with_funding),
                expectancy_with_funding=sum(with_funding)/len(with_funding) if with_funding else None,
                net_with_funding=math.prod(1+v for v in with_funding)-1 if with_funding else None)


def v1(series, now):
    market = dict(instrument=INST, series={tf: series[tf].legacy(now) for tf in ("15m", "1H", "4H")})
    result = technical_v1(market, {}, now, reference_price=market["series"].get("15m",{}).get("bars",[[0,0,0,0,None]])[-1][4])
    if result.get("scenario"):
        return dict(candidate=result["candidate"], plan=result["scenario"], evaluation_bar="1m",
                    holding_seconds=21600, status=result["status"])
    return result


def regime(frames):
    hour = frames["1H"]
    if not hour:
        return "unknown"
    if hour["atr_ratio"] > 1.35:
        return "high_volatility"
    score = hour["trend"]
    return "bullish" if score >= 3 else "bearish" if score <= -3 else "range"


def report(series, rates=None):
    common_start = max(series["1m"].opens[0], *(series[tf].opens[0] + 50*series[tf].step for tf in FRAMES))
    end = min(s.opens[-1]+s.step for s in series.values() if s.tf in ("1m","5m"))
    span = end - common_start
    if span <= 0:
        raise ValueError("Insufficient overlapping data")
    boundaries = [math.ceil((common_start + span*f)/3600)*3600 for f in (0,.60,.80)] + [end]
    results = {}
    for fold, lo, hi in zip(("development", "forward_1", "holdout"), boundaries, boundaries[1:]):
        groups, active, excluded, diagnostic = {}, {}, Counter(), Counter()
        for now in range(lo, hi, 300):
            frames = {tf: source.current(now) for tf, source in series.items()}
            signals = evaluate(frames, now, historical=True)["strategies"]
            if now % 900 == 0:
                signals["TECHNICAL_V1"] = v1(series, now)
            mode = regime(frames)
            for model, signal in signals.items():
                if model in SPECS and now % (BARS[SPECS[model][1]] // 1000):
                    continue
                if "plan" not in signal:
                    reason = (signal.get("blockers") or ["no_closed_trigger"])[0]
                    excluded[model + ":" + reason] += 1
                    # Descriptive diagnostic, calculated after the decision and isolated from replay.
                    f = frames["5m"]
                    future = series["5m"].slice(now, min(hi, now+1800))
                    if f and len(future)==6 and all(b[0]==now+i*300 for i,b in enumerate(future)):
                        for direction, excursion in (("LONG",max(b[2] for b in future)-f["close"]),
                                                    ("SHORT",f["close"]-min(b[3] for b in future))):
                            if excursion >= 2*f["atr"]:
                                diagnostic[model+":"+reason+":"+direction] += 1
                    continue
                source = series[signal["evaluation_bar"]]
                for validity in SWING_VALIDITY if model == "SWING_V2" else SHORT_VALIDITY:
                    key = f"{model}:{validity}"
                    if now < active.get(key,0):
                        excluded[model+":overlap"] += 1
                        continue
                    if now + source.step + validity + signal["holding_seconds"] > hi:
                        continue
                    item = simulate(source, signal, now, validity)
                    item["funding_adjustment"] = funding_adjustment(item, rates, source)
                    active[key] = item.get("ended_at", item["cursor"] + source.step)
                    direction = item["direction"]
                    for suffix in ("all",direction,mode,direction+":"+mode,signal["status"]):
                        groups.setdefault(key+":"+suffix, []).append(item)
        results[fold] = dict(start=lo, end=hi, results={k:stats(v) for k,v in groups.items()},
                             exclusions=excluded, excursions_after_exclusion=diagnostic)
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--now", type=int)
    parser.add_argument("--funding", type=Path)
    args = parser.parse_args()
    import time
    now = args.now or int(time.time())
    if args.fetch:
        fetch_history(args.data, now)
    series, coverage = {}, {}
    for tf in FRAMES:
        path = args.data / f"gold-full-{tf}.ndjson"
        rows = load_history(path, tf, now)
        step = BARS[tf]//1000
        coverage[tf] = dict(n=len(rows), first=rows[0][0], last_close=rows[-1][0]+step,
                            missing=sum((b[0]-a[0])//step-1 for a,b in zip(rows,rows[1:])),
                            pagination_finished=path.with_suffix(".complete").exists(),
                            zero_volume=sum(row[5]==0 for row in rows))
        series[tf] = Series(rows,tf)
    rates = {int(t)//1000:float(v) for t,v in json.loads(args.funding.read_text()).items()} if args.funding else {}
    if any(not math.isfinite(rate) or t % FUNDING_STEP for t,rate in rates.items()):
        raise ValueError("Invalid realized funding history")
    result = dict(instrument=INST, coverage=coverage, fee_rates=FEES, folds=report(series,rates),
                  funding=dict(n=len(rates),first=min(rates,default=None),last=max(rates,default=None),
                               settlement_mark="closed_contract_price_proxy"))
    args.output.write_text(json.dumps(result,indent=2,allow_nan=False))
    print(json.dumps(coverage,indent=2))


if __name__ == "__main__":
    main()
