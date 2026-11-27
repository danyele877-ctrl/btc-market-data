"""Research calendar cache; unverified timestamps never generate countdowns."""
import json
from datetime import datetime,timezone
from pathlib import Path
from gold_calendar_sources import probe,SOURCES
from gold_calendar import fetch
from gold_calendar_unified import normalize
from gold_tracking import save

def refresh(root,now=None):
    now=now or datetime.now(timezone.utc)
    path=Path(root)/"calendar.json"
    old=json.loads(path.read_text()) if path.exists() else {}
    try:
        age=(now-datetime.fromisoformat(old["retrieved_at"])).total_seconds()
    except (KeyError,ValueError,TypeError): age=1e9
    if old.get("schema_version") != 2 or not 0<=age<3600:
        sources=dict(probe(item) for item in SOURCES.items())
        old=normalize(sources,now,fetch())
        old["retrieved_at"]=now.isoformat()
        old["schema_version"]=2
        save(path,old)
    result=dict(old)
    result["events"]=[]
    for event in old.get("events",[]):
        e=dict(event)
        e.pop("seconds_to_event",None); e.pop("phase",None)
        if e.get("status")=="TIMESTAMP_PARSED" and e.get("scheduled_at"):
            delta=(datetime.fromisoformat(e["scheduled_at"])-now).total_seconds()
            if delta < -3600: continue
            e["seconds_to_event"]=round(delta)
            e["phase"]="POST_RELEASE" if delta<=0 else "IMMINENT" if delta<=1800 else "UPCOMING"
        result["events"].append(e)
    save(Path(root)/"calendar_view.json",result)
    return result
