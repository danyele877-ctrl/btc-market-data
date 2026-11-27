"""Read-only endpoints for GOLD RADAR."""
import gzip
import json
import logging
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from gold_backtest import return_after_fees, stats
from gold_paper import summarize
from gold_quotes import snapshot

PERIODS = {"24h": 86400, "7d": 604800, "30d": 2592000, "all": None}
FILTERS = {"strategy": "model", "direction": "direction", "horizon": "horizon", "state": "state", "cohort": "cohort"}


def paper_page(ledger, query, now):
    order = sorted(ledger.values(), key=lambda row: row["observed_at"], reverse=True)
    rows = [dict(row, horizon=row.get("horizon", "SWING" if row["model"] == "SWING" else "INTRADAY"))
            for row in order]
    period = PERIODS[query.get("period", ["all"])[0]]
    page = max(1, int(query.get("page", ["1"])[0]))
    selected = {key: query.get(key, [""])[0] for key in FILTERS}
    matched = [row for row in rows if (period is None or now - row["observed_at"] <= period)
               and all(not selected[key] or row[field] == selected[key] for key, field in FILTERS.items())]
    groups = {}
    for row in reversed(matched):
        groups.setdefault(row["model"] + ":" + row["cohort"], []).append(row)
    metrics = {}
    for group, samples in groups.items():
        # Return series use one non-overlapping proposal per strategy and cohort.
        last_end = 0
        returns = []
        for item in samples:
            if item["observed_at"] < last_end:
                continue
            value = return_after_fees(item)
            if value is not None:
                returns.append(item)
                last_end = item["ended_at"]
        metrics[group] = stats(returns)
        equity = 1.
        high = 1.
        curve = [[0, 0]]
        for item in returns:
            equity *= 1 + return_after_fees(item)
            high = max(high, equity)
            curve.append([equity-1, equity/high-1])
        stride = max(1, len(curve)//120)
        metrics[group].update(curve=curve[::stride] + ([curve[-1]] if (len(curve)-1)%stride else []),
                              frequency=Counter(row["direction"] for row in samples))
    result = summarize({i: row for i, row in enumerate(matched)})
    result.pop("recent")
    options = {key: sorted({row[field] for row in rows}) for key, field in FILTERS.items()}
    return dict(**result, rows=matched[(page-1)*30:page*30], page=page, page_size=30,
                options=options, metrics=metrics)


def serve(root, port=8080):
    root = Path(root)
    html = Path(__file__).with_name("gold_web.html").read_bytes()

    compressed = None, None

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def do_GET(self):
            nonlocal compressed
            encoding = None
            target = urlsplit(self.path)
            path = target.path
            code, kind = 200, "application/json"
            try:
                if path == "/":
                    payload, kind = html, "text/html; charset=utf-8"
                elif path == "/healthz":
                    payload = b'{"web":"ok"}'
                elif path == "/dashboard.json":
                    payload = (root/"dashboard.json").read_bytes()
                    data = json.loads(payload)
                    if not 0 <= time.time() - float(data["generated_at"]) <= 90:
                        raise ValueError("stale snapshot")
                elif path == "/live.json":
                    data = snapshot(root, time.time())
                    if not data["book"] and not data["last_trade"]:
                        code = 503
                    payload = json.dumps(data, separators=(",", ":"), allow_nan=False).encode()
                elif path == "/paper.json":
                    query = parse_qs(target.query)
                    try:
                        data = paper_page(json.loads((root/"paper_ledger.json").read_text()), query, time.time())
                    except (KeyError, ValueError):
                        code, data = 400, {"error":"invalid filter or ledger"}
                    payload = json.dumps(data, separators=(",", ":"), allow_nan=False).encode()
                else:
                    code, payload = 404, b'{"error":"not found"}'
            except (OSError, ValueError, KeyError, TypeError):
                code, payload = 503, b'{"error":"dashboard unavailable"}'
            if code == 200 and path == "/dashboard.json" and "gzip" in self.headers.get("Accept-Encoding",""):
                stamp = data["generated_at"]
                candidate = compressed
                if candidate[0] != stamp:
                    candidate = stamp, gzip.compress(payload, compresslevel=2)
                    compressed = candidate
                payload, encoding = candidate[1], "gzip"
            self.send_response(code)
            self.send_header("Content-Type",kind)
            self.send_header("Content-Length",str(len(payload)))
            self.send_header("Cache-Control","no-store")
            if path == "/dashboard.json":
                self.send_header("Vary","Accept-Encoding")
            if encoding:
                self.send_header("Content-Encoding",encoding)
            self.send_header("X-Content-Type-Options","nosniff")
            self.send_header("Content-Security-Policy","default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; connect-src 'self'")
            self.end_headers()
            try:
                self.wfile.write(payload)
            except OSError:
                pass

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("0.0.0.0", port), Handler) as server:
        logging.info("Gold dashboard listening on %s", port)
        server.serve_forever()
