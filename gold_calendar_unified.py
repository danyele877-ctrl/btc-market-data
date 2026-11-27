"""Research calendar: date-only Fed and timezone-unverified BEA events."""
import json,re,sys
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from gold_calendar import fetch as fetch_bls

def normalize(sources,now,bls=None):
    events=[]
    for name in ("FED","BEA"):
        src=sources.get(name,{})
        if src.get("http_status")!=200 or not src.get("source_html"): continue
        soup=BeautifulSoup(src["source_html"],"html.parser")
        if name=="FED":
            for panel in soup.select(".panel"):
                heading=panel.select_one(".panel-heading")
                match=re.search(r"(\d{4}) FOMC Meetings",heading.get_text(" ",strip=True)) if heading else None
                if not match: continue
                for row in panel.select(".fomc-meeting"):
                    m=row.select_one(".fomc-meeting__month"); d=row.select_one(".fomc-meeting__date")
                    if not m or not d: continue
                    days=re.fullmatch(r"(\d{1,2})-(\d{1,2})(\*)?",d.get_text(strip=True))
                    if not days: continue
                    try:
                        end=datetime.strptime(f"{match[1]} {m.get_text(strip=True)} {days[2]}","%Y %B %d").date()
                        start=end.replace(day=int(days[1]))
                    except ValueError: continue
                    if end>=now.date():
                        events.append(dict(kind="FOMC",source=name,date=end.isoformat(),start_date=start.isoformat(),projections=bool(days[3]),scheduled_at=None,status="DATE_ONLY",retrieved_at=src.get("retrieved_at")))
        else:
            for row in soup.select("tr.scheduled-releases-type-press"):
                title=row.select_one(".release-title"); date=row.select_one(".release-date"); clock=row.select_one(".scheduled-date small")
                if not title or not date or not clock: continue
                title=title.get_text(" ",strip=True)
                kind="PCE" if "Personal Income and Outlays" in title else "GDP" if ("Gross Domestic Product" in title or re.match(r"^GDP \((?:Advance|Second|Third) Estimate\)",title,re.I)) else None
                if not kind: continue
                display_date, display_time = date.get_text(strip=True), clock.get_text(strip=True)
                table = row.find_parent("table")
                year = table.find("th", string=re.compile(r"^Year 20\d{2}$")) if table else None
                scheduled, status = None, "YEAR_AND_TIMEZONE_UNVERIFIED"
                if year and src.get("source", "").startswith("https://www.bea.gov/news/schedule"):
                    try:
                        local = datetime.strptime(f"{year.get_text(strip=True)} {display_date} {display_time}", "Year %Y %B %d %I:%M %p")
                        scheduled = local.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc).isoformat()
                        status = "TIMESTAMP_PARSED"
                    except ValueError:
                        pass
                events.append(dict(kind=kind,source=name,title=title,display_date=display_date,display_time=display_time,scheduled_at=scheduled,status=status,retrieved_at=src.get("retrieved_at")))
    bls=bls or {}
    if bls.get("status")=="RESEARCH_ONLY":
        for event in bls.get("events",[]):
            events.append(dict(event,source="BLS",status="TIMESTAMP_PARSED",retrieved_at=bls.get("retrieved_at")))
    return {"research_only":True,"notifications_enabled":False,"events":events,
            "coverage":{"BLS":bls.get("status","UNAVAILABLE"),**{name:{"status":sources.get(name,{}).get("status","UNAVAILABLE"),"matched_events":sum(e["source"]==name for e in events)} for name in ("FED","BEA")}}}

if __name__=="__main__":
    with open(sys.argv[1]) as f: sources=json.load(f)
    print(json.dumps(normalize(sources,datetime.now(timezone.utc),fetch_bls()),indent=2))
