"""Detect the first aligned BTC/ZEC impulse on closed OKX Europe 1m candles."""

from math import isfinite
from statistics import median

from render_bridge_publication import BTC_ID, _timestamp
from zec_macro_shadow import ZEC_STREAM

VOLUME_WINDOW = 30
QUIET_MINUTES = 10
MAX_CLOSE_AGE = 45


def first_impulse(instruments, now):
    close_at = int(now.timestamp()) // 60 * 60
    if now.timestamp() - close_at >= MAX_CLOSE_AGE:
        return None

    lookback = VOLUME_WINDOW + QUIET_MINUTES
    start = close_at - lookback * 60
    series = []
    for instrument in (BTC_ID, ZEC_STREAM):
        rows = {}
        for row in instruments.get(instrument, {}).get("1m", ())[-(lookback + 2):]:
            try:
                closed = _timestamp(row["completed_at"]).timestamp()
                opening, close, volume = (float(row[key]) for key in ("open", "close", "volume_quote"))
            except (KeyError, TypeError, ValueError):
                continue
            if all(isfinite(value) for value in (opening, close, volume)) and min(opening, close) > 0 and volume >= 0:
                rows[closed] = (100 * (close / opening - 1), volume, close)
        if any(t not in rows for t in range(start, close_at + 1, 60)):
            return None
        series.append([rows[t] for t in range(start, close_at + 1, 60)])

    btc, zec = series

    def sample(index):
        b, bv, _ = btc[index]
        z, zv, price = zec[index]
        b_base, z_base = (median(row[1] for row in asset[index-VOLUME_WINDOW:index]) for asset in series)
        sign = 1 if b > 0 else -1
        if (sign * b < 0.3 or sign * z < 0.6 or min(b_base, z_base) <= 0
                or min(bv / b_base, zv / z_base) < 3):
            return None
        return {"side": "LONG" if sign > 0 else "SHORT", "btc_pct": b, "zec_pct": z, "zec_close": price,
                "btc_volume_multiple": bv / b_base, "zec_volume_multiple": zv / z_base}

    impulse = sample(lookback)
    if impulse is None:
        return None
    # Suppress a second pulse in the same direction during the first ten minutes.
    for index in range(VOLUME_WINDOW, lookback):
        prior = sample(index)
        if prior and prior["side"] == impulse["side"]:
            return None
    return {**impulse, "closed_at": close_at}
