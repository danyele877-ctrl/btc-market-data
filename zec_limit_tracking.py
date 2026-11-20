"""Persist prospective, paired LIMIT simulations using closed OKX Europe 1m bars."""

import json
from datetime import timezone
from math import ceil, floor, isfinite

from read_current_alert import validated_side
from render_bridge_publication import ZEC_ID, _timestamp
from telegram_alert import limit_levels


def simulate(plan, bars):
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
        filled = filled or adverse <= sign * plan["entry"]
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


class LimitTracker:
    def __init__(self, connect):
        self.connect = connect
        self.state = {"status": "starting", "records": []}

    def update(self, compact, slot, candles, rows, now):
        try:
            self._update(compact, slot, candles, rows, now)
        except Exception:
            # Database errors may contain connection details. Do not affect live signals.
            self.state = {**self.state, "status": "unavailable"}
            print("ZEC LIMIT simulation storage unavailable", flush=True)

    def _update(self, compact, slot, candles, rows, now):
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
                signal = _timestamp(compact["generated_at"]).astimezone(timezone.utc)
                capture = _timestamp(compact["captured_at"])
                expiry = _timestamp(compact["feed"]["expires_at"])
                quote = compact["prices"][ZEC_ID]["last"]
                invalidation = compact["decision"]["invalidation_price"]
                plans = []
                for variant, fraction in (("BASELINE", 0.25), ("SHADOW_NEAR", 0.125), ("SHADOW_FAR", 0.5)):
                    levels, reason = limit_levels(side, quote, invalidation, candles, capture, fraction)
                    plans.append({**(levels or {}), "variant": variant, "side": side, "reason": reason,
                                  "quote": quote, "invalidation": invalidation,
                                  "signal_at": signal.timestamp(),
                                  "fill_by": min(signal.timestamp() + 300, expiry.timestamp()),
                                  "outcome_by": signal.timestamp() + 2700})
                db.execute(f"INSERT INTO zec_limit_trials VALUES ({p},{p},{p},NULL) ON CONFLICT DO NOTHING",
                           (slot, ceil(signal.timestamp()) + 2760, json.dumps(plans)))
            if bars:
                due = db.execute(f"SELECT slot, plans FROM zec_limit_trials WHERE outcomes IS NULL AND due <= {p}",
                                 (int(max(bars)),)).fetchall()
                for key, payload in due:
                    outcomes = [simulate(plan, bars) if not plan["reason"] else {"status": "blocked"}
                                for plan in json.loads(payload)]
                    db.execute(f"UPDATE zec_limit_trials SET outcomes={p} WHERE slot={p} AND outcomes IS NULL",
                               (json.dumps(outcomes), key))
            recent = db.execute("SELECT plans, outcomes FROM zec_limit_trials ORDER BY slot DESC LIMIT 100").fetchall()
        self.state = {"status": "ok", "records": [
            {"plans": json.loads(plans), "outcomes": json.loads(outcomes) if outcomes else None}
            for plans, outcomes in recent]}
