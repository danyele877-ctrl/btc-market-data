"""Measure Telegram pre-breakout notices against subsequent closed OKX 1m candles."""

import json
from math import ceil, floor


def ensure_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS zec_prebreakout_notices (
        slot BIGINT PRIMARY KEY, accepted_at DOUBLE PRECISION NOT NULL, payload TEXT NOT NULL,
        outcome_5m TEXT, outcome_15m TEXT
    )""")


def outcome(observation, accepted_at, minutes, bars):
    deadline = accepted_at + minutes * 60
    first_close = (floor(accepted_at / 60) + 1) * 60
    last_close = ceil(deadline / 60) * 60
    closes = range(first_close, last_close + 1, 60)
    for close in closes:
        if close not in bars:
            return {"status": "missing_data"}
        low, high = bars[close][:2]
        if observation["side"] == "LONG":
            breakout, reversal = high >= observation["level"], low <= observation["invalidation"]
        else:
            breakout, reversal = low <= observation["level"], high >= observation["invalidation"]
        if not (breakout or reversal):
            continue
        if close - 60 < accepted_at or close > deadline or (breakout and reversal):
            return {"status": "ambiguous"}
        return {"status": "breakout" if breakout else "invalidation",
                "elapsed_s_bounds": [round(close - 60 - accepted_at, 1), round(close - accepted_at, 1)]}
    return {"status": "no_touch"}


def update_notices(db, param, bars):
    ensure_table(db)
    latest = max(bars, default=0)
    notices = db.execute("""SELECT slot, accepted_at, payload, outcome_5m, outcome_15m
        FROM zec_prebreakout_notices ORDER BY accepted_at DESC LIMIT 100""").fetchall()
    records = []
    for slot, accepted_at, payload, saved_5m, saved_15m in notices:
        observation = json.loads(payload)
        outcomes = {}
        for horizon, saved in ((5, saved_5m), (15, saved_15m)):
            result = json.loads(saved) if saved else None
            if result is None and latest >= accepted_at + horizon * 60:
                candidate = outcome(observation, accepted_at, horizon, bars)
                if candidate["status"] != "missing_data" or latest >= accepted_at + horizon * 60 + 300:
                    column = f"outcome_{horizon}m"
                    db.execute(f"UPDATE zec_prebreakout_notices SET {column}={param} "
                               f"WHERE slot={param} AND {column} IS NULL", (json.dumps(candidate), slot))
                    result = candidate
            outcomes[f"{horizon}m"] = result
        records.append({**observation, "telegram_accepted_at": accepted_at, "outcomes": outcomes})
    return records
