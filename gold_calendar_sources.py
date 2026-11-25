"""Calendar source diagnostics; no actionable timestamps inferred from HTML."""
import json, re, urllib.request
from html.parser import HTMLParser
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

SOURCES={"FED":"https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm","BEA":"https://www.bea.gov/news/schedule"}

class Extract(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts=[]; self.skip=0
    def handle_starttag(self,tag,attrs):
        if tag in ("script","style"): self.skip+=1
        if tag in ("tr","div","p","h2","h3","li","br"): self.parts.append("\n")
        if tag in ("td","th"): self.parts.append(" | ")
    def handle_endtag(self,tag):
        if tag in ("script","style"): self.skip=max(0,self.skip-1)
        if tag in ("tr","div","p","li"): self.parts.append("\n")
    def handle_data(self,data):
        if not self.skip: self.parts.append(data)

def extract(raw,source):
    p=Extract(); p.feed(raw)
    lines=[" ".join(x.split()) for x in "".join(p.parts).splitlines()]
    lines=[x for x in lines if x]
    pattern=r"personal income and outlays|gross domestic product" if source=="BEA" else r"FOMC|meeting|press conference"
    hits=[i for i,x in enumerate(lines) if re.search(pattern,x,re.I)]
    indices=sorted({j for i in hits for j in range(max(0,i-2),min(len(lines),i+3))})
    return {"matched_lines":len(hits),"excerpts":[lines[i][:500] for i in indices[:200]],"events":[],"validated":False}

def probe(item):
    name,url=item
    out={"source":url,"retrieved_at":datetime.now(timezone.utc).isoformat(),"validated":False,"events":[]}
    try:
        req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0 GoldRadarResearch/1.0","Accept":"text/html"})
        with urllib.request.urlopen(req,timeout=20) as r:
            body=r.read(2000001); out["http_status"]=r.status
        if len(body)>2000000: raise ValueError("response too large")
        raw=body.decode("utf-8",errors="replace")
        out.update(extract(raw,name))
        # Preserve source markup for offline verification of date/title associations.
        # Public source only; never execute the markup.
        out["source_html"]=raw
        import hashlib
        out["source_sha256"]=hashlib.sha256(body).hexdigest()
        out["schema_version"]=2
        out["status"]="STRUCTURE_REVIEW_REQUIRED" if out["matched_lines"] else "NO_MATCH"
    except Exception as e:
        out.update(status="UNAVAILABLE",error=str(e))
    return name,out

if __name__=="__main__":
    with ThreadPoolExecutor(max_workers=2) as pool:
        print(json.dumps(dict(pool.map(probe,SOURCES.items())),indent=2))
