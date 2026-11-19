#!/usr/bin/env python3
"""Read the versioned compact radar alert or report current OKX quotes only."""

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.request import Request, urlopen

from render_bridge_publication import ALERT_MAX_AGE, BTC_ID, ZEC_ID, ZEC_SPOT_ID, _positive_price, _timestamp

PRIMARY_URL = "https://p01--btc-zec-radar--db9jws2f6wdk.code.run/radar-alert.json"
CONTENTS_URL = "https://api.github.com/repos/danyele877-ctrl/btc-market-data/contents/data/radar_alert.json?ref=main"
TICKER_URL = "https://eea.okx.com/api/v5/market/ticker?instId="
INSTRUMENTS = (BTC_ID, ZEC_SPOT_ID, ZEC_ID)


def get_json(url):
    request = Request(url, headers={
        "Accept": "application/json", "Cache-Control": "no-cache",
        "User-Agent": "btc-zec-radar-reader/1.0",
    })
    with urlopen(request, timeout=12) as response:
        return json.load(response)


def snapshot_is_current(document, now):
    feed = document["feed"]
    captured = _timestamp(document["captured_at"])
    generated = _timestamp(document["generated_at"])
    expires = _timestamp(feed["expires_at"])
    return (
        document["schema_version"] == 1 and feed["status"] == "ok"
        and feed["fresh_at_publication"] is True
        and all((captured, generated, expires))
        and captured <= generated <= now < expires <= captured + ALERT_MAX_AGE
    )


def validated_side(document, now):
    if not snapshot_is_current(document, now):
        return "NO_SIGNAL"
    quality, decision = document["quality"], document["decision"]
    side = decision["side"]
    level = _positive_price(decision.get("invalidation_price"))
    if (
        quality["engine_ready"] is not True or quality["required_prices_available"] is not True
        or decision["reason"] != "confirmed_trigger" or side not in ("LONG", "SHORT") or level is None
    ):
        return "NO_SIGNAL"
    quotes = document["prices"]
    if not all(_positive_price(quotes[instrument]["last"]) for instrument in INSTRUMENTS):
        return "NO_SIGNAL"
    derivative = quotes[ZEC_ID]
    prices = [_positive_price(derivative["last"])]
    if derivative.get("mark") is not None:
        prices.append(_positive_price(derivative["mark"]))
    if all(price is not None and (price > level if side == "LONG" else price < level) for price in prices):
        return side
    return "NO_SIGNAL"


def read_ticker(instrument):
    try:
        payload = get_json(TICKER_URL + instrument)
        row, = payload["data"]
        price = _positive_price(row["last"])
        observed = datetime.fromtimestamp(int(row["ts"]) / 1000, tz=timezone.utc)
        if payload["code"] == "0" and row["instId"] == instrument and price is not None:
            return price, observed
    except (OSError, KeyError, TypeError, ValueError, OverflowError):
        pass
    return None


def read_current_alert():
    """Prefer the live compact Northflank document, then the GitHub mirror.

    A stale or invalid primary is never mixed with the fallback document.
    An intact, fresh NO_SIGNAL is authoritative and must not be replaced.
    """
    reason = "snapshot_unavailable"
    for source in ("NORTHFLANK", "GITHUB"):
        try:
            if source == "NORTHFLANK":
                document = get_json(PRIMARY_URL)
                blob_sha = None
            else:
                contents = get_json(CONTENTS_URL)
                if contents["encoding"] != "base64":
                    raise ValueError("unexpected GitHub encoding")
                document = json.loads(base64.b64decode(contents["content"], validate=True))
                blob_sha = contents["sha"]
            now = datetime.now(timezone.utc)
            if isinstance(document, dict) and snapshot_is_current(document, now):
                side = validated_side(document, now)
                return {"source": source, "blob_sha": blob_sha,
                        "snapshot": document, "decision": side}
            reason = source.lower() + "_snapshot_stale_or_invalid"
        except (OSError, KeyError, TypeError, ValueError, AttributeError, IndexError):
            reason = source.lower() + "_snapshot_unavailable"

    with ThreadPoolExecutor(max_workers=len(INSTRUMENTS)) as executor:
        observations = dict(zip(INSTRUMENTS, executor.map(read_ticker, INSTRUMENTS)))
    now = datetime.now(timezone.utc)
    prices = {}
    for instrument, quote in observations.items():
        if quote is None:
            continue
        price, observed = quote
        if now - ALERT_MAX_AGE <= observed <= now:
            prices[instrument] = {"last": price, "observed_at": observed.isoformat()}
    return {"source": "OKX_EEA_REST_QUOTES", "reason": reason,
            "prices": prices, "decision": "NO_SIGNAL"}


if __name__ == "__main__":
    print(json.dumps(read_current_alert(), allow_nan=False))
