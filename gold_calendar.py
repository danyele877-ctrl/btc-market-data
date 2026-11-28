"""BLS scheduled releases only; no inference of release values or price direction."""
import json, urllib.request, re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

URL="https://www.bls.gov/schedule/news_release/bls.ics"

def parse(raw, now):
    raw=re.sub(r"\r?\n[ \t]","",raw)
    events=[]
    for block in raw.split("BEGIN:VEVENT")[1:]:
        fields={}
        for line in block.splitlines():
            if line=="END:VEVENT": break
            if ":" in line:
                k,v=line.split(":",1); fields[k]=v
        if fields.get("STATUS")=="CANCELLED": continue
        title=fields.get("SUMMARY","")
        low=title.lower()
        kind="CPI" if "consumer price index" in low else "NFP" if "employment situation" in low else None
        if not kind: continue
        for k,v in fields.items():
            if not k.startswith("DTSTART"): continue
            try:
                if v.endswith("Z"): dt=datetime.strptime(v,"%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                elif k in ("DTSTART;TZID=America/New_York", "DTSTART;TZID=US-Eastern", "DTSTART;TZID=US/Eastern"): dt=datetime.strptime(v,"%Y%m%dT%H%M%S").replace(tzinfo=ZoneInfo("America/New_York"))
                else: continue
            except ValueError: continue
            delta=(dt-now).total_seconds()
            if -3600<=delta<=45*86400:
                events.append(dict(kind=kind,title=title,scheduled_at=dt.astimezone(timezone.utc).isoformat(),seconds_to_event=round(delta),phase="POST_RELEASE" if delta<=0 else "IMMINENT" if delta<=1800 else "UPCOMING",uid=fields.get("UID")))
    return sorted(events,key=lambda x:x["scheduled_at"])

def fetch(now=None):
    now=now or datetime.now(timezone.utc)
    try:
        req=urllib.request.Request(URL,headers={"User-Agent":"GoldRadarDiagnostic/1.0","Accept":"text/calendar"})
        with urllib.request.urlopen(req,timeout=20) as r: raw=r.read(2000001)
        if len(raw)>2000000 or b"BEGIN:VCALENDAR" not in raw or b"END:VCALENDAR" not in raw: raise ValueError("invalid calendar")
        return dict(source=URL,retrieved_at=now.isoformat(),status="RESEARCH_ONLY",events=parse(raw.decode("utf-8-sig"),now),coverage=["CPI","NFP"],missing=["FOMC","PCE"],direction=None)
    except Exception as e:
        return dict(source=URL,retrieved_at=now.isoformat(),status="UNAVAILABLE",error=str(e),events=[],direction=None)

if __name__=="__main__":
    print(json.dumps(fetch(),indent=2))
