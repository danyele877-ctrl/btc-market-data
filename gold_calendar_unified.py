"""Research calendar: date-only Fed and timezone-unverified BEA events."""
import json,re,sys
from datetime import datetime,timezone
from bs4 import BeautifulSoup
from gold_calendar import parse

def normalize(sources,now):
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
                kind="PCE" if "Personal Income and Outlays" in title else "GDP" if "Gross Domestic Product" in title else None
                if not kind: continue
                events.append(dict(kind=kind,source=name,title=title,display_date=date.get_text(strip=True),display_time=clock.get_text(strip=True),scheduled_at=None,status="YEAR_AND_TIMEZONE_UNVERIFIED",retrieved_at=src.get("retrieved_at")))
    return {"research_only":True,"notifications_enabled":False,"events":events}

if __name__=="__main__":
    with open(sys.argv[1]) as f: sources=json.load(f)
    print(json.dumps(normalize(sources,datetime.now(timezone.utc)),indent=2))
