"""Candle-based research ledger. Evidence of a price crossing cannot verify an exchange fill."""
import json
import math
from collections import Counter
from pathlib import Path

from gold_tracking import save

ACTIVE = {"PENDING", "TOUCHED", "DATA_GAP"}
LEGACY_REPLAY = {"DATA_GAP", "STOP_OR_AMBIGUOUS"}
TARGETS = ("tp1", "tp2", "tp3")


def start_at(item):
    step = item["step"]
    return (int(item["observed_at"]) // step + 1) * step


def resume_state(item):
    return "TOUCHED" if "filled_at" in item else "PENDING"


def valid_bar(bar, step):
    t, o, h, l, c, v = bar
    return (all(math.isfinite(x) for x in bar) and t % step == 0 and
            0 < l <= min(o, c) <= max(o, c) <= h and v >= 0)


def advance(item, bars, now, historical_complete=False):
    """Advance only through a contiguous prefix of fully closed bars."""
    plan = item["plan"]
    long = item["direction"] == "LONG"
    step = item["step"]
    targets = [key for key in TARGETS if plan.get(key) is not None]
    if item["state"] not in ACTIVE:
        return
    item["state"] = resume_state(item)
    ordered = {}
    conflicts = set()
    for bar in bars:
        t = bar[0]
        if t in ordered and tuple(ordered[t]) != tuple(bar):
            conflicts.add(t)
        ordered[t] = bar
    cursor = item["cursor"]
    while cursor + step <= now:
        state = item["state"]
        deadline = (item["expires_at"] if state == "PENDING"
                    else item["filled_at"] + item["holding_seconds"])
        if cursor + step > deadline:
            item.update(state="EXPIRED" if state == "PENDING" else "TIMEOUT", ended_at=deadline)
            return
        bar = ordered.get(cursor)
        if bar is None or cursor in conflicts or not valid_bar(bar, step):
            if historical_complete or any(t > cursor and t + step <= now for t in ordered):
                item.update(state="DATA_GAP", missing_at=cursor)
            return
        cursor += step
        item["cursor"] = cursor
        item.pop("missing_at", None)
        _, opening, high, low, close, volume = bar
        if state == "TOUCHED":
            item["last_close"] = close
        if volume > 0:
            stop = low <= plan["stop"] if long else high >= plan["stop"]
            hits = [k for k in targets if (high >= plan[k] if long else low <= plan[k])
                    and k not in item.get("targets_touched", {})]
            if state == "PENDING":
                entry = plan["entry_limit"]
                crossed = low < entry if long else high > entry
                invalidation = plan.get("invalidation", plan["stop"])
                invalidated = low <= invalidation if long else high >= invalidation
                if crossed:
                    item["entry_crossed_at"] = cursor
                if crossed and stop:
                    item.update(state="AMBIGUOUS", ended_at=cursor, ambiguity="entry_and_stop")
                    return
                if invalidated and (not crossed or invalidation != plan["stop"]):
                    item.update(state="AMBIGUOUS" if crossed else "INVALIDATED", ended_at=cursor)
                    if crossed:
                        item["ambiguity"] = "entry_and_invalidation"
                    return
                if crossed:
                    item.update(state="TOUCHED", filled_at=cursor, last_close=close,
                                entry_evidence="traded_bar_crossing", fill_verified=False)
                    if hits:
                        item["unsequenced_entry_targets"] = hits
            elif stop:
                # An opening already through the stop orders the stop before later extremes.
                stop_at_open = opening <= plan["stop"] if long else opening >= plan["stop"]
                ambiguous = bool(hits and not stop_at_open)
                item.update(state="AMBIGUOUS" if ambiguous else "STOP",
                            ended_at=cursor, exit_price=opening if stop_at_open else plan["stop"])
                if ambiguous:
                    item["ambiguity"] = "target_and_stop"
                    item.pop("exit_price", None)
                return
            else:
                for key in hits:
                    item.setdefault("targets_touched", {})[key] = cursor
                if targets[-1] in item.get("targets_touched", {}):
                    item.update(state="FINAL_TARGET_TOUCHED", ended_at=cursor, exit_price=plan[targets[-1]])
                    return
        if cursor == deadline:
            item.update(state="EXPIRED" if state == "PENDING" else "TIMEOUT", ended_at=deadline)
            return


def replay_legacy(item, bars, now):
    if item.get("schema") == 2 or item.get("legacy_outcome") or item["state"] not in LEGACY_REPLAY:
        return False
    replay = {k: v for k, v in item.items() if k not in ("filled_at", "targets_touched", "live_check", "entry_crossed_at")}
    replay.update(state="PENDING", cursor=start_at(item))
    advance(replay, bars, now, historical_complete=True)
    if replay["state"] == "DATA_GAP":
        return False
    replay["legacy_outcome"] = {k: item[k] for k in ("state", "cursor", "filled_at", "targets_touched") if k in item}
    item.clear()
    item.update(replay)
    return True


def update(root, technical, swing, market, now, quote=None, strategies=None):
    root = Path(root)
    path = root / "paper_ledger.json"
    ledger = json.loads(path.read_text()) if path.exists() else {}
    series = market.get("series", {})
    try:
        repair = json.loads((root / "paper_history.json").read_text())
    except (OSError, ValueError):
        repair = {}
    windows = repair.get("windows", {}) if repair.get("instrument") == market.get("instrument") else {}
    for key, item in ledger.items():
        local = series.get(item["bar"], {}).get("bars", [])
        window = windows.get(key, {})
        rest = window.get("bars", [])
        # REST is independently validated and overlapping values must agree.
        bars = local + rest
        if key in windows and replay_legacy(item, bars, now):
            continue
        if item["state"] in ACTIVE and (item["state"] != "DATA_GAP" or item.get("schema") == 2):
            advance(item, bars, now, historical_complete=window.get("through", 0) > item["cursor"])

    live_book = None
    try:
        if quote and quote.get("connected") and 0 <= now - float(quote["observed_at"]) <= 5:
            book = quote["book"]
            bid, ask = float(book["bids"][0][0]), float(book["asks"][0][0])
            if 0 <= now - int(book["ts"]) / 1000 <= 5 and 0 < bid <= ask and all(map(math.isfinite, (bid, ask))):
                live_book = bid, ask
    except (KeyError, TypeError, ValueError, IndexError):
        pass

    def add(model, direction, plan, bar, ref, validity, holding, cohort, blockers, horizon):
        if direction not in ("LONG", "SHORT") or not plan or not all(k in plan for k in ("entry_limit", "stop", "tp1")):
            return
        key = f"{model}:{int(ref)}:{direction}"
        if key in ledger:
            return
        if any(x["model"] == model and x["direction"] == direction and x["state"] in ("PENDING", "TOUCHED")
               for x in ledger.values()):
            return
        step = 60 if bar == "1m" else 300
        start = (int(now) // step + 1) * step
        ledger[key] = {"model": model, "direction": direction, "plan": plan, "observed_at": now,
                       "expires_at": start + validity, "holding_seconds": holding, "state": "PENDING",
                       "cursor": start, "step": step, "bar": bar, "cohort": cohort,
                       "blockers": blockers, "horizon": horizon, "schema": 2}

    if technical.get("status") in ("PAPER_TEST_CANDIDATE", "CONDITIONAL_WATCH") and series.get("15m"):
        add("TECHNICAL", technical["candidate"], technical.get("scenario"), "1m", series["15m"]["bar_end"],
            300, 21600, technical["status"], technical.get("blockers", []), "INTRADAY")
    if swing.get("candidate") in ("LONG", "SHORT") and series.get("4H"):
        direction = swing["candidate"]
        add("SWING", direction, swing.get("plans", {}).get(direction), "5m", series["4H"]["bar_end"],
            21600, 259200, "STRUCTURAL_ONLY", ["funding_unverified", "macro_unverified"], "SWING")
    for model, signal in (strategies or {}).items():
        if signal.get("status") in ("PAPER_TEST_CANDIDATE", "CONDITIONAL_WATCH"):
            add(model, signal["candidate"], signal["plan"], signal["evaluation_bar"], signal["reference"],
                signal["plan"]["validity_seconds"], signal["holding_seconds"], signal["status"],
                signal["blockers"], signal["horizon"])

    for item in ledger.values():
        if item["state"] not in ("PENDING", "TOUCHED"):
            continue
        item["live_check"] = {"status": "UNAVAILABLE", "observed_at": now}
        if live_book:
            bid, ask = live_book
            p = item["plan"]
            long = item["direction"] == "LONG"
            stop = bid <= p.get("invalidation", p["stop"]) if long else ask >= p.get("invalidation", p["stop"])
            entry = ask <= p["entry_limit"] if long else bid >= p["entry_limit"]
            status = "INVALIDATION_ZONE" if stop else "ENTRY_ZONE" if entry and item["state"] == "PENDING" else "MONITORING"
            if item["state"] == "PENDING" and now >= item["expires_at"]:
                status = "EXPIRED_AWAITING_CANDLES"
            item["live_check"] = dict(status=status, observed_at=now, bid=bid, ask=ask)
    save(path, ledger)
    return summarize(ledger)


def summarize(ledger):
    counts, targets, blocked, directions = {}, {}, {}, {}
    for item in ledger.values():
        group = item["model"] + ":" + item["cohort"]
        counts.setdefault(group, Counter())[item["state"]] += 1
        directions.setdefault(group, Counter())[item["direction"]] += 1
        targets.setdefault(group, Counter()).update(item.get("targets_touched", {}).keys())
        blocked.setdefault(group, Counter()).update(item.get("blockers", []))
    return {"research_only": True, "fill_verified": False, "counts": counts, "directions": directions,
            "targets": targets, "blocked_reasons": blocked, "total": len(ledger),
            "recent": sorted(ledger.values(), key=lambda x: x["observed_at"], reverse=True)[:12]}
