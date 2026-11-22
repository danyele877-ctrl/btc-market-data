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


def shadow_microstructure(health, side):
    """Record the diagnostic inputs available at the first candidate, before any outcome."""
    book = health.get("book") or {}
    cvd = health.get("cvd") or {}
    book_age = health.get("book_age_seconds")
    trade_age = health.get("trade_age_seconds")
    imbalance = book.get("imbalance_top10")
    delta = cvd.get("1m")

    def finite_number(value):
        return type(value) in (int, float) and isfinite(value)

    ready = (
        health.get("connected") is True and health.get("book_valid") is True
        and health.get("book_gap_count") == 0
        and health.get("trade_events_truncated") is False
        and cvd.get("ready_1m") is True and cvd.get("gap_count_1m") == 0
        and all(finite_number(age) and 0 <= age <= 30 for age in (book_age, trade_age))
        and finite_number(imbalance) and -1 <= imbalance <= 1 and finite_number(delta)
    )
    sign = 1 if side == "LONG" else -1
    return {
        "book_age_s": book_age if finite_number(book_age) else None,
        "trade_age_s": trade_age if finite_number(trade_age) else None,
        "book_imbalance_top10": imbalance if finite_number(imbalance) else None,
        "cvd_1m_contracts": delta if finite_number(delta) else None,
        "aligned": (sign * imbalance > 0 and sign * delta > 0) if ready else None,
    }


def shadow_prebreakout(plan, closed, minutes, health):
    """Classify the same first SHADOW candidate using only pre-candidate data."""
    signal = plan["signal_at"]
    slot_start = plan["slot"] * 300
    anchor = floor(signal / 60) * 60
    sign = 1 if plan["side"] == "LONG" else -1
    result = {"version": 1, "qualified": None}
    try:
        window = [row for row in closed if _timestamp(row["completed_at"]).timestamp() <= slot_start][-8:]
        reference = next(row for row in reversed(minutes)
                         if _timestamp(row["completed_at"]).timestamp() == anchor-300)
        highs = [float(row["high"]) for row in window]
        lows = [float(row["low"]) for row in window]
        typical = sum(high-low for high, low in zip(highs, lows)) / len(window)
        level = max(highs) if sign > 0 else min(lows)
        distance = sign * (level-plan["quote"]) / typical
        prior_move = sign * (plan["quote"]-float(reference["close"])) / typical
        previous_book = next(row for row in reversed(health.get("book_history", ()))
                             if 15 <= signal-row["ts_ms"]/1000 <= 30)
        book_change = sign * (plan["microstructure"]["book_imbalance_top10"]-previous_book["imbalance_top10"])
        previous, recent = [], []
        for row in health.get("trade_events", ()):
            age = signal-row["ts_ms"]/1000
            if 0 <= age < 60:
                (recent if age < 30 else previous).append(sign * row["delta_contracts"])
    except (ValueError, TypeError, KeyError, AttributeError, StopIteration, ZeroDivisionError):
        return result

    if not all(isfinite(value) for value in (distance, prior_move, book_change)):
        return result
    result.update(level=level, distance_in_ranges=round(distance, 4),
                  prior_move_in_ranges=round(prior_move, 4), book_change=round(book_change, 6))
    aligned = plan["microstructure"]["aligned"]
    if aligned is None or not previous or not recent:
        return result
    flow_change = sum(recent)-sum(previous)
    result.update(flow_change_contracts=round(flow_change, 4),
                  qualified=bool(aligned and book_change > 0 and sum(recent) > 0 and flow_change > 0
                                 and 0 <= distance <= 0.5 and prior_move <= 0.5))
    return result


def shadow_directional_moves(plan, bars):
    """Use only contiguous closed 1m observations around the captured trade."""
    anchor = floor(plan["signal_at"] / 60) * 60
    quote = plan["quote"]
    sign = 1 if plan["side"] == "LONG" else -1
    moves = {}
    for label, offset in (("prior_5m", -5), ("after_5m", 5), ("after_15m", 15)):
        start, end = sorted((0, offset))
        if not all(anchor + index * 60 in bars for index in range(start, end + 1)):
            moves[label] = None
            continue
        close = bars[anchor + offset * 60][2]
        change = quote / close - 1 if offset < 0 else close / quote - 1
        moves[label] = round(sign * change * 100, 6)
    return moves


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
                    outcome = {**simulate(plan, bars), "strict_status": simulate(plan, bars, strict=True)["status"],
                               "directional_move_pct": shadow_directional_moves(plan, bars)}
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
