"""Persist prospective, paired LIMIT simulations using closed OKX Europe 1m bars."""

import json
from decimal import Decimal, ROUND_HALF_UP
from math import ceil, floor, isfinite

from read_current_alert import validated_side
from render_bridge_publication import ZEC_ID, _timestamp
from telegram_alert import limit_levels


def build_limit_plans(compact, slot, candles):
    side = compact["decision"]["side"]
    signal = _timestamp(compact["generated_at"]).timestamp()
    capture = _timestamp(compact["captured_at"])
    expiry = _timestamp(compact["feed"]["expires_at"]).timestamp()
    quote = compact["prices"][ZEC_ID]["last"]
    invalidation = compact["decision"]["invalidation_price"]
    return [
        {**(levels or {}), "id": f"ZEC-{slot}-{variant}", "variant": variant, "side": side,
         "reason": reason, "quote": quote, "invalidation": invalidation, "signal_at": signal,
         "fill_by": min(capture.timestamp()+300, expiry), "outcome_by": signal+2700}
        for variant, fraction, retest in (("BASELINE", .25, False), ("SHADOW_NEAR", .125, False),
                                         ("SHADOW_FAR", .5, False), ("SHADOW_RETEST", .25, True))
        for levels, reason in [limit_levels(side, quote, invalidation, candles, capture, fraction, retest)]
    ]


def simulate(plan, bars, *, strict=False):
    # Only full post-signal minutes count. Never infer a fill from an overlapping bar.
    start = ceil(plan["signal_at"] / 60) * 60
    fill_by = floor(plan["fill_by"] / 60) * 60
    end = floor(plan["outcome_by"] / 60) * 60
    filled = False
    sign = 1 if plan["side"] == "LONG" else -1
    for opened in range(start, end, 60):
        if not filled and opened >= fill_by:
            return {"status": "unfilled", "gross_r": 0}
        bar = bars.get(opened + 60)
        if bar is None:
            return {"status": "missing_data", "filled": filled}
        low, high, close = bar
        adverse, favorable = (low, high) if sign > 0 else (-high, -low)
        already_filled = filled
        entry = sign * plan["entry"]
        filled = filled or (adverse < entry if strict else adverse <= entry)
        if filled:
            if adverse <= sign * plan["stop"]:
                return {"status": "stop", "filled": True, "gross_r": -1}
            if already_filled and favorable >= sign * plan["tp1"]:
                exit_price = plan["tp1"]
                status = "tp1"
                break
    else:
        if not filled:
            return {"status": "unfilled", "gross_r": 0}
        exit_price, status = close, "time_exit"
    return {"status": status, "filled": True,
            "gross_r": round(sign * (exit_price - plan["entry"]) / abs(plan["entry"] - plan["stop"]), 4)}


def shadow_live_plan(now, closed, trades):
    """Freeze the first sampled candidate using eight closes and the open 5m bar."""
    signal = now.timestamp()
    start = int(signal) // 300 * 300
    try:
        window = [row for row in closed if _timestamp(row["completed_at"]).timestamp() <= start][-8:]
        if len(window) != 8 or any(_timestamp(row["completed_at"]).timestamp() != start - (7-i)*300
                                   for i, row in enumerate(window)):
            return None
        prices = sorted((row["ts_ms"], row["trade_id"], float(row["price"])) for row in trades
                        if start*1000 <= row["ts_ms"] <= signal*1000)
        if not prices or signal - prices[-1][0]/1000 > 30:
            return None
        quotes = [price for _, _, price in prices]
        if not all(isfinite(price) and price > 0 for price in quotes):
            return None
        movement = quotes[-1] - float(window[0]["close"])
        average = (sum(float(row["high"]) - float(row["low"]) for row in window)
                   + max(quotes) - min(quotes)) / 9
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    if not movement or not isfinite(movement) or not isfinite(average) or average <= 0:
        return None
    sign = 1 if movement > 0 else -1
    def tick(value):
        return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    entry = tick(quotes[-1] - sign * average / 4)
    unit = tick(average)
    if unit <= 0 or entry <= 2.5 * unit or (quotes[-1] - entry) * sign <= 0:
        return None
    return {"slot": start // 300, "side": "LONG" if sign > 0 else "SHORT",
            "entry": entry, "stop": tick(entry - sign*tick(unit*1.5)),
            "tp1": tick(entry + sign*unit*1.5), "tp2": tick(entry + sign*unit*2.5),
            "quote": quotes[-1], "signal_at": signal, "fill_by": signal + 300,
            "outcome_by": signal + 2700}


class LimitTracker:
    def __init__(self, connect):
        self.connect = connect
        self.state = {"status": "starting", "records": []}

    def update(self, compact, slot, candles, rows, now, shadow=None):
        try:
            self._update(compact, slot, candles, rows, now, shadow)
        except Exception:
            # Database errors may contain connection details. Do not affect live signals.
            self.state = {**self.state, "status": "unavailable"}
            print("ZEC LIMIT simulation storage unavailable", flush=True)

    def _update(self, compact, slot, candles, rows, now, shadow=None):
        bars = {}
        for row in rows:
            try:
                end = _timestamp(row["completed_at"]).timestamp()
                low, high, close = (float(row[key]) for key in ("low", "high", "close"))
                if end <= now.timestamp() and 0 < low <= close <= high and isfinite(high):
                    bars[end] = low, high, close
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
        with self.connect() as (db, p):
            db.execute("""CREATE TABLE IF NOT EXISTS zec_limit_trials (
                slot BIGINT PRIMARY KEY, due BIGINT NOT NULL, plans TEXT NOT NULL, outcomes TEXT
            )""")
            try:
                side = validated_side(compact, now) if slot is not None else None
            except (KeyError, TypeError, ValueError):
                side = None
            if side in ("LONG", "SHORT") and slot is not None:
                plans = compact.get("limit_plans") or build_limit_plans(compact, slot, candles)
                db.execute(f"INSERT INTO zec_limit_trials VALUES ({p},{p},{p},NULL) ON CONFLICT DO NOTHING",
                           (slot, ceil(plans[0]["outcome_by"]) + 60, json.dumps(plans)))
            if bars:
                due = db.execute(f"SELECT slot, plans FROM zec_limit_trials WHERE outcomes IS NULL AND due <= {p}",
                                 (int(max(bars)),)).fetchall()
                for key, payload in due:
                    outcomes = [{**simulate(plan, bars), "strict_status": simulate(plan, bars, strict=True)["status"]}
                                if not plan["reason"] else {"status": "blocked"}
                                for plan in json.loads(payload)]
                    db.execute(f"UPDATE zec_limit_trials SET outcomes={p} WHERE slot={p} AND outcomes IS NULL",
                               (json.dumps(outcomes), key))
            db.execute("""CREATE TABLE IF NOT EXISTS zec_shadow_live_trials (
                slot BIGINT PRIMARY KEY, due BIGINT NOT NULL, plan TEXT NOT NULL,
                outcomes TEXT, baseline_at DOUBLE PRECISION
            )""")
            if shadow:
                db.execute(f"""INSERT INTO zec_shadow_live_trials (slot, due, plan)
                    VALUES ({p},{p},{p}) ON CONFLICT DO NOTHING""",
                           (shadow["slot"], ceil(shadow["outcome_by"]), json.dumps(shadow)))
            if side in ("LONG", "SHORT") and slot is not None:
                confirmed_at = _timestamp(compact["generated_at"]).timestamp()
                candidate = db.execute(f"SELECT plan FROM zec_shadow_live_trials WHERE slot={p}",
                                       (slot-1,)).fetchone()
                if candidate and json.loads(candidate[0])["side"] == side:
                    db.execute(f"""UPDATE zec_shadow_live_trials SET baseline_at={p}
                        WHERE slot={p} AND baseline_at IS NULL""", (confirmed_at, slot-1))
            if bars:
                due_shadow = db.execute(f"""SELECT slot, plan FROM zec_shadow_live_trials
                    WHERE outcomes IS NULL AND due <= {p}""", (int(max(bars)),)).fetchall()
                for key, payload in due_shadow:
                    plan = json.loads(payload)
                    outcome = {**simulate(plan, bars), "strict_status": simulate(plan, bars, strict=True)["status"]}
                    db.execute(f"UPDATE zec_shadow_live_trials SET outcomes={p} WHERE slot={p} AND outcomes IS NULL",
                               (json.dumps(outcome), key))
            recent_shadow = db.execute("""SELECT plan, outcomes, baseline_at
                FROM zec_shadow_live_trials ORDER BY slot DESC LIMIT 100""").fetchall()
            recent = db.execute("SELECT plans, outcomes FROM zec_limit_trials ORDER BY slot DESC LIMIT 100").fetchall()
        self.state = {"status": "ok", "records": [
            {"plans": json.loads(plans), "outcomes": json.loads(outcomes) if outcomes else None}
            for plans, outcomes in recent], "shadow_records": [
            {"plan": json.loads(plan), "outcome": json.loads(outcomes) if outcomes else None,
             "baseline_at": baseline_at} for plan, outcomes, baseline_at in recent_shadow]}
