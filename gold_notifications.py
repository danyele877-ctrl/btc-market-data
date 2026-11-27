"""Gold-only, persistent notification queue. Sends messages, never orders."""
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from gold_quotes import load
from gold_tracking import save

TZ = ZoneInfo("Europe/Rome")
MAX_EVENT_AGE = 600


def settings():
    token = os.getenv("GOLD_TELEGRAM_BOT_TOKEN", "")
    chat = os.getenv("GOLD_TELEGRAM_CHAT_ID", "")
    if re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token) and re.fullmatch(r"-?\d+|@[A-Za-z0-9_]+", chat):
        return token, chat
    return None


def events(ledger, dashboard, now, conditional=False):
    for key, item in ledger.items():
        if item["cohort"] != "PAPER_TEST_CANDIDATE" and not conditional:
            continue
        if item.get("legacy_outcome") or item.get("schema") != 2:
            continue
        direction = item["direction"]
        plan = item["plan"]
        label = f"🟡 GOLD · {direction} · {item['model'].replace('_', ' ')}"
        suffix = "\nSimulazione · Europe/Rome"
        stamp = item["observed_at"]
        market_time = datetime.fromtimestamp(stamp,TZ).strftime("%H:%M")
        expiry = datetime.fromtimestamp(item["expires_at"],TZ).strftime("%d/%m %H:%M")
        levels = " · ".join(f"{k.upper()} {plan[k]:g}" for k in ("tp1","tp2","tp3") if k in plan)
        conditional_label = " · condizionato" if item["cohort"] != "PAPER_TEST_CANDIDATE" else ""
        yield key+":setup", stamp, f"{label}\n{market_time} · Nuovo setup{conditional_label}\nLimit {plan['entry_limit']:g} · SL {plan['stop']:g}\n{levels}\nScade {expiry}"+suffix
        live = item.get("live_check", {})
        if item["state"] in ("PENDING","TOUCHED") and live.get("status") == "INVALIDATION_ZONE":
            yield key+":live_warning", live["observed_at"], f"{label}\n⚠️ Zona di invalidazione sul book · livello {plan['stop']:g}"+suffix
        if "filled_at" in item:
            yield key+":entry",item["filled_at"],f"{label}\n🎯 Entry {plan['entry_limit']:g} attraversata nel modello"+suffix
        for target, when in item.get("targets_touched", {}).items():
            yield key+":"+target,when,f"{label}\n✅ {target.upper()} {plan[target]:g} raggiunto"+suffix
        if item["state"] in ("INVALIDATED","STOP","AMBIGUOUS") and "ended_at" in item:
            reason = {"INVALIDATED":"❌ Setup invalidato", "STOP":"🛑 Stop Loss confermato",
                      "AMBIGUOUS":"⚠️ Sequenza intrabar incerta"}[item["state"]]
            yield key+":end",item["ended_at"],f"{label}\n{reason} · SL {plan['stop']:g}"+suffix
    calendar = dashboard.get("calendar") or {}
    if calendar.get("fresh"):
        for event in (calendar.get("data") or {}).get("events", []):
            kind = event.get("kind")
            stamp = event.get("scheduled_at")
            if stamp and kind in {"FOMC","CPI","PCE","GDP","NFP"} and event.get("status") == "TIMESTAMP_PARSED":
                actual = datetime.fromisoformat(stamp).timestamp()
                for offset, label in ((1800,"Tra 30 min"),(0,"Orario di pubblicazione")):
                    yield f"macro:{kind}:{stamp}:{offset}", actual-offset, f"📅 GOLD · {label} · {kind}\n{datetime.fromtimestamp(actual,TZ):%d/%m %H:%M} Italia · {event['source']}"
            elif kind == "FOMC" and event.get("date"):
                local = datetime.fromisoformat(event["date"]+"T09:00:00").replace(tzinfo=TZ)
                yield f"macro:FOMC:{event['date']}:date", local.timestamp(), "📅 GOLD · Riunione Fed oggi\nOrario da verificare sul calendario ufficiale"
    for article in dashboard.get("news",{}).get("items",[]):
        if article["official"]:
            key=hashlib.sha256(article["url"].encode()).hexdigest()
            yield f"news:{key}", article["published_at"], f"🇺🇸 GOLD · Federal Reserve\n{article['title']}\n{article['url']}"


def bias_event(state, dashboard, now):
    candidate = dashboard.get("scenarios", {}).get("5–15 min")
    if candidate not in ("LONG","SHORT") or now-dashboard.get("generated_at",0)>35:
        state.pop("provisional_bias",None)
        return None
    previous = state.get("provisional_bias")
    frame = dashboard.get("market_view",{}).get("data") or {}
    frame_end = frame.get("series",{}).get("1m",{}).get("bar_end")
    if frame_end is None:
        return None
    if not previous or previous["direction"] != candidate:
        state["provisional_bias"] = {"direction":candidate,"bar_end":frame_end}
        return None
    if frame_end <= previous["bar_end"]:
        return None
    stable = state.get("bias")
    if stable == candidate or now-state.get("bias_at",0)<900:
        return None
    state.update(bias=candidate,bias_at=now)
    if stable:
        return f"bias:{candidate}:{frame_end}", now, f"↔️ GOLD · Cambio 5–15 min\n{stable} → {candidate} · due chiusure coerenti"
    return None


def send(credentials, message):
    token, chat = credentials
    req=Request(f"https://api.telegram.org/bot{token}/sendMessage",
                data=json.dumps({"chat_id":chat,"text":message,"disable_web_page_preview":True}).encode(),
                headers={"Content-Type":"application/json"},method="POST")
    try:
        with urlopen(req,timeout=8) as response:
            return "SENT" if json.load(response).get("ok") else "REJECTED",0
    except HTTPError as exc:
        if exc.code==429:
            data=json.loads(exc.read(4096))
            return "RETRY",min(600,max(1,int(data.get("parameters",{}).get("retry_after",30))))
        return "REJECTED" if 400<=exc.code<500 else "UNCERTAIN",0
    except (OSError,ValueError):
        return "UNCERTAIN",0


def worker(root, credentials):
    root=Path(root)
    conditional=os.getenv("GOLD_TELEGRAM_INCLUDE_CONDITIONAL","0")=="1"
    path=root/"telegram_outbox.json"
    state=load(path) or {"started_at":time.time(),"messages":{}}
    save(path,state)
    while True:
        now=time.time()
        try:
            dashboard=load(root/"dashboard.json")
            ledger=load(root/"paper_ledger.json")
            messages=state["messages"]
            old_bias = (state.get("provisional_bias"),state.get("bias"))
            event_list=list(events(ledger,dashboard,now,conditional))
            event=bias_event(state,dashboard,now)
            if event:
                event_list.append(event)
            changed=old_bias!=(state.get("provisional_bias"),state.get("bias"))
            for key, when, text in event_list:
                freshness=900 if key.startswith("news:") else MAX_EVENT_AGE
                if key not in messages and max(now-freshness,state["started_at"]) <= when <= now:
                    messages[key]=dict(when=when,text=text,state="PENDING",retry_at=now)
                    changed=True
            for key, event in list(messages.items()):
                if now-event["when"]>7*86400:
                    del messages[key]
                    changed=True
            if changed:
                save(path,state)
            pending=sorted((e for e in messages.values() if e["state"] in ("PENDING","RETRY")
                            and e["retry_at"]<=now and now-e["when"]<=900),key=lambda e:e["when"])
            if pending:
                event=pending[0]
                # Persist the attempt before dispatch. A crash or network failure leaves
                # an uncertain delivery instead of risking duplicate notifications.
                event["state"]="UNCERTAIN"
                save(path,state)
                status, delay=send(credentials,event["text"])
                event.update(state=status,retry_at=now+delay)
                save(path,state)
        except Exception as exc:
            logging.warning("Gold Telegram processing failed (%s)",type(exc).__name__)
        time.sleep(5)
