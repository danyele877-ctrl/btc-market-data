"""Public-market research dashboard only. No arbitrary file access."""
import json,time,logging
from http.server import HTTPServer,BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

def serve(root):
    root=Path(root)
    html=Path(__file__).with_name("gold_web.html").read_bytes()
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)
        def do_GET(self):
            try:
                path=urlsplit(self.path).path
                if path=="/":
                    body=html; typ="text/html; charset=utf-8"; code=200
                elif path=="/dashboard.json":
                    data=json.loads((root/"dashboard.json").read_text())
                    age=time.time()-float(data["generated_at"])
                    if not 0<=age<=90: raise ValueError("stale snapshot")
                    body=json.dumps(data,allow_nan=False).encode(); typ="application/json"; code=200
                elif self.path=="/healthz":
                    body=b'{"web":"ok"}'; typ="application/json"; code=200
                else:
                    body=b"Not found"; typ="text/plain"; code=404
            except (OSError,ValueError,KeyError,TypeError):
                body=b'{"error":"dashboard unavailable"}'; typ="application/json"; code=503
            self.send_response(code)
            self.send_header("Content-Type",typ)
            self.send_header("Content-Length",str(len(body)))
            self.send_header("Cache-Control","no-store")
            self.send_header("X-Content-Type-Options","nosniff")
            self.send_header("Content-Security-Policy","default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; connect-src 'self'")
            self.end_headers()
            try: self.wfile.write(body)
            except OSError: pass
        def log_message(self,*args): pass
    with HTTPServer(("0.0.0.0",8080),Handler) as server:
        logging.info("Gold dashboard listening on 8080")
        server.serve_forever()
