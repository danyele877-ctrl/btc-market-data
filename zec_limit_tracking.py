"""Persist prospective, paired LIMIT simulations using closed OKX Europe 1m bars."""

import json
from decimal import Decimal, ROUND_HALF_UP
from math import ceil, floor, isfinite

from read_current_alert import validated_side
from render_bridge_publication import ZEC_ID, _timestamp
from telegram_alert import limit_levels
from zec_limit_costs import ADVERSE_EXIT_SLIPPAGE_BPS, execution_pnl, limit_economics
from zec_prebreakout_tracking import update_notices


def build_limit_plans(compact, slot, candles):
    side = compact["decision"]["side"]
    signal = _timestamp(compact["generated_at"]).timestamp()
    capture = _timestamp(compact["captured_at"])
    expiry = _timestamp(compact["feed"]["expires_at"]).timestamp()
    quote = compact["prices"][ZEC_ID]["last"]
    invalidation = compact["decision"]["invalidation_price"]
    plans = [
        {**(levels or {}), "id": f"ZEC-{slot}-{variant}", "variant": variant, "side": side,
         "reason": reason, "quote": quote, "invalidation": invalidation, "signal_at": signal,
         "fill_by": min(capture.timestamp()+300, expiry), "outcome_by": signal+2700}
        for variant, fraction, retest in (("BASELINE", .25, False), ("SHADOW_NEAR", .125, False),
                                         ("SHADOW_FAR", .5, False), ("SHADOW_RETEST", .25, True))
        for levels, reason in [limit_levels(side, quote, invalidation, candles, capture, fraction, retest)]
    ]

    reference = plans[0]
    for plan in plans:
        if not plan["reason"]:
            plan["economics"] = limit_economics(plan)
    if reference["reason"]:
        return plans
    sign = 1 if side == "LONG" else -1
    entry, stop, tp1 = (reference[key] for key in ("entry", "stop", "tp1"))
    risk = abs(entry-stop)
    buffer = max(0.01, round(risk/3, 2))
    wide_stop = round(sign * min(sign*stop-0.01, sign*invalidation-buffer), 2)
    wide_tp1 = round(entry + 1.5*(tp1-entry), 2)
    for variant, trial_stop, trial_tp1 in (
        ("SHADOW_EXIT_CONTROL", stop, tp1),
        ("SHADOW_STOP_BUFFER", wide_stop, tp1),
        ("SHADOW_TARGET_ONLY", stop, wide_tp1),
        ("SHADOW_STOP_TARGET", wide_stop, wide_tp1),
    ):
        if not 0 < min(trial_stop, trial_tp1) or sign*trial_tp1 >= sign*reference["tp2"]:
            continue
        trial = {**reference, "id": f"ZEC-{slot}-{variant}", "variant": variant,
                 "stop": trial_stop, "tp1": trial_tp1, "reference_risk": risk}
        trial["economics"] = limit_economics(trial)
        plans.append(trial)
    return plans


def simulate(plan, bars, *, strict=False):
    # Only full post-signal minutes count. Never infer a fill from an overlapping bar.
    start = ceil(plan["signal_at"] / 60) * 60
    fill_by = floor(plan["fill_by"] / 60) * 60
    end = floor(plan["outcome_by"] / 60) * 60
    filled = False
    sign = 1 if plan["side"] == "LONG" else -1
    comparison = "reference_risk" in plan

    def result(status, exit_price=None):
        if exit_price is None:
            outcome = {"status": status, "gross_r": 0}
            if comparison and status in ("unfilled", "invalidated"):
                outcome.update(net_reference_r=0, net_equal_risk_r=0,
                               net_reference_r_with_slippage=0, net_equal_risk_r_with_slippage=0)
            return outcome
        pnl = sign * (exit_price - plan["entry"])
        outcome = {"status": status, "filled": True,
                   "gross_r": round(pnl / abs(plan["entry"]-plan["stop"]), 4)}
        if comparison:
            net, _ = execution_pnl(plan["side"], plan["entry"], exit_price)
            slippage = 0 if status == "tp1" else ADVERSE_EXIT_SLIPPAGE_BPS
            stressed, _ = execution_pnl(plan["side"], plan["entry"], exit_price, exit_slippage_bps=slippage)
            risk = abs(plan["entry"] - plan["stop"])
            outcome["net_reference_r"] = round(net / plan["reference_risk"], 4)
            outcome["net_equal_risk_r"] = round(net / risk, 4)
            outcome["net_reference_r_with_slippage"] = round(stressed / plan["reference_risk"], 4)
            outcome["net_equal_risk_r_with_slippage"] = round(stressed / risk, 4)
        return outcome

    for opened in range(start, end, 60):
        if not filled and opened >= fill_by:
            return result("unfilled")
        bar = bars.get(opened + 60)
        if bar is None:
            return {"status": "missing_data", "filled": filled}
        low, high, close = bar[:3]
        adverse, favorable = (low, high) if sign > 0 else (-high, -low)
        already_filled = filled
        entry = sign * plan["entry"]
        touched = adverse < entry if strict else adverse <= entry
        if comparison and not filled and adverse <= sign * plan["invalidation"]:
            opening = bar[3] if len(bar) > 3 else None
            if not touched or (opening is not None and sign*opening <= sign*plan["invalidation"]):
                return result("invalidated")
            opening_touches = opening is not None and (sign*opening < entry if strict else sign*opening <= entry)
            if not opening_touches:
                # OHLC cannot order intraminute entry and invalidation.
                return {"status": "ambiguous_entry"}
        filled = filled or touched
        if filled:
            if adverse <= sign * plan["stop"]:
                return result("stop", plan["stop"])
            if already_filled and favorable >= sign * plan["tp1"]:
                exit_price = plan["tp1"]
                status = "tp1"
                break
    else:
        if not filled:
            return result("unfilled")
        exit_price, status = close, "time_exit"
    return result(status, exit_price)


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
    result.update(level=level, typical_range=typical, distance_in_ranges=round(distance, 4),
                  prior_move_in_ranges=round(prior_move, 4), book_change=round(book_change, 6))
    aligned = plan["microstructure"]["aligned"]
    if aligned is None or not previous or not recent:
        return result
    flow_change = sum(recent)-sum(previous)
    result.update(flow_change_contracts=round(flow_change, 4),
                  qualified=bool(aligned and book_change > 0 and sum(recent) > 0 and flow_change > 0
                                 and 0 <= distance <= 0.5 and prior_move <= 0.5))
    return result


def live_prebreakout(now, closed, minutes, health):
    """Require a continuous tape and an untouched level in the current 5m bar."""
    cvd = health.get("cvd") or {}
    trade_age = health.get("trade_age_seconds")
    if cvd.get("ready_5m") is not True or trade_age is None or not 0 <= trade_age <= 10:
        return None
    events = health.get("trade_events", ())
    plan = shadow_live_plan(now, closed, events)
    if not plan:
        return None
    plan["microstructure"] = shadow_microstructure(health, plan["side"])
    setup = shadow_prebreakout(plan, closed, minutes, health)
    if setup["qualified"] is not True:
        return None
    sign = 1 if plan["side"] == "LONG" else -1
    level = setup["level"]
    crossed = any(sign * (float(trade["price"]) - level) >= 0 for trade in events
                  if plan["slot"] * 300_000 <= trade["ts_ms"] <= plan["signal_at"] * 1000)
    if crossed:
        return None
    invalidation = round(plan["quote"] - sign * setup["typical_range"] / 2, 2)
    if invalidation <= 0 or sign * (plan["quote"] - invalidation) <= 0:
        return None
    return {"side": plan["side"], "observed_at": plan["signal_at"], "price": plan["quote"],
            "level": level, "invalidation": invalidation}


def directional_moves(plan, bars):
    """Use only contiguous closed 1m observations around the captured trade."""
    anchor = floor(plan["signal_at"] / 60) * 60
    quote = plan["quote"]
    sign = 1 if plan["side"] == "LONG" else -1
    moves = {}
    for label, offset in (("prior_5m", -5), ("after_5m", 5), ("after_15m", 15),
                          ("after_30m", 30), ("after_45m", 45)):
        start, end = sorted((0, offset))
        if not all(anchor + index * 60 in bars for index in range(start, end + 1)):
            moves[label] = None
            continue
        close = bars[anchor + offset * 60][2]
        change = quote / close - 1 if offset < 0 else close / quote - 1
        moves[label] = round(sign * change * 100, 6)
    return moves


def snapshot_moves(plan, outcome, bars):
    """Add newly available horizons without replacing stored measurements."""
    signal, quote = plan.get("signal_at"), plan.get("quote")
    if (not bars or not all(type(value) in (int, float) and isfinite(value) and value > 0
                            for value in (signal, quote)) or plan.get("side") not in ("LONG", "SHORT")
            or floor(signal / 60) * 60 not in bars):
        return False
    stored = outcome.get("directional_move_pct", {})
    if all(stored.get(key) is not None for key in ("after_5m", "after_15m", "after_30m", "after_45m")):
        return False
    new = {key: value for key, value in directional_moves(plan, bars).items()
           if value is not None and stored.get(key) is None}
    if not new:
        return False
    outcome["directional_move_pct"] = {**stored, **new}
    return True


class LimitTracker:
    def __init__(self, connect, *, prebreakout_only=False):
        self.connect = connect
        self.prebreakout_only = prebreakout_only
        self.state = {"status": "starting", "records": []}

    def update(self, compact, slot, candles, rows, now, shadow=None, macro=None):
        try:
            self._update(compact, slot, candles, rows, now, shadow, macro)
        except Exception:
            # Database errors may contain connection details. Do not affect live signals.
            self.state = {**self.state, "status": "unavailable"}
            print("ZEC LIMIT simulation storage unavailable", flush=True)

    def _update(self, compact, slot, candles, rows, now, shadow=None, macro=None):
        bars = {}
        for row in rows:
            try:
                end = _timestamp(row["completed_at"]).timestamp()
                low, high, close = (float(row[key]) for key in ("low", "high", "close"))
                if end <= now.timestamp() and 0 < low <= close <= high and isfinite(high):
                    try:
                        opening = float(row.get("open"))
                    except (TypeError, ValueError):
                        opening = float("nan")
                    bars[end] = low, high, close, opening if low <= opening <= high else None
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
        with self.connect() as (db, p):
            prebreakouts = update_notices(db, p, bars)
            if self.prebreakout_only:
                self.state = {"status": "ok", "records": [], "prebreakout_notices": prebreakouts}
                return
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
                    plans = json.loads(payload)
                    outcomes = [{**simulate(plan, bars), "strict_status": simulate(plan, bars, strict=True)["status"]}
                                if not plan["reason"] else {"status": "blocked"} for plan in plans]
                    for plan, outcome in zip(plans, outcomes):
                        if plan.get("variant") == "BASELINE":
                            snapshot_moves(plan, outcome, bars)
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
                    snapshot_moves(plan, outcome, bars)
                    db.execute(f"UPDATE zec_shadow_live_trials SET outcomes={p} WHERE slot={p} AND outcomes IS NULL",
                               (json.dumps(outcome), key))
            db.execute("""CREATE TABLE IF NOT EXISTS zec_macro_observations (
                event_id TEXT NOT NULL, phase TEXT NOT NULL,
                observed_at DOUBLE PRECISION NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (event_id, phase)
            )""")
            if macro and macro.get("event"):
                event_id = macro["event"]["id"]
                phase = macro["status"]
                observations = [(event_id, phase, macro["observed_at"], json.dumps(macro))]
                if side in ("LONG", "SHORT"):
                    signal_at = _timestamp(compact["generated_at"]).timestamp()
                    if macro["pause_from"] <= signal_at < macro["watch_until"]:
                        observation = {"side": side, "signal_at": signal_at,
                                       "in_macro_pause": signal_at < macro["pause_until"]}
                        observations.append((event_id, f"baseline_{slot}", signal_at, json.dumps(observation)))
                for observation in observations:
                    db.execute(f"INSERT INTO zec_macro_observations VALUES ({p},{p},{p},{p}) ON CONFLICT DO NOTHING",
                               observation)
            recent_macro = db.execute("""SELECT event_id, phase, payload FROM zec_macro_observations
                ORDER BY observed_at DESC LIMIT 100""").fetchall()
            recent_shadow = []
            for key, payload, saved, baseline_at in db.execute("""SELECT slot, plan, outcomes, baseline_at
                FROM zec_shadow_live_trials ORDER BY slot DESC LIMIT 100""").fetchall():
                plan = json.loads(payload)
                outcome = json.loads(saved) if saved else None
                if outcome is not None and snapshot_moves(plan, outcome, bars):
                    db.execute(f"UPDATE zec_shadow_live_trials SET outcomes={p} WHERE slot={p} AND outcomes={p}",
                               (json.dumps(outcome), key, saved))
                recent_shadow.append({"plan": plan, "outcome": outcome, "baseline_at": baseline_at})
            recent = []
            for key, payload, saved in db.execute("""SELECT slot, plans, outcomes
                FROM zec_limit_trials ORDER BY slot DESC LIMIT 100""").fetchall():
                plans = json.loads(payload)
                outcomes = json.loads(saved) if saved else None
                if outcomes is not None:
                    for plan, outcome in zip(plans, outcomes):
                        if plan.get("variant") == "BASELINE" and snapshot_moves(plan, outcome, bars):
                            db.execute(f"UPDATE zec_limit_trials SET outcomes={p} WHERE slot={p} AND outcomes={p}",
                                       (json.dumps(outcomes), key, saved))
                recent.append({"plans": plans, "outcomes": outcomes})
        self.state = {"status": "ok", "records": recent, "shadow_records": recent_shadow,
            "prebreakout_notices": prebreakouts,
            "macro_records": [{"event_id": event_id, "phase": phase, "observation": json.loads(payload)}
                              for event_id, phase, payload in recent_macro]}
