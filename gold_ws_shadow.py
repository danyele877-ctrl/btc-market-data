"""Independent websocket diagnostics on the existing persistent volume."""
import asyncio,time,logging
from pathlib import Path
from gold_ws_feed import Feed,run
from gold_tracking import save
from gold_history import metadata

def worker(root):
    path=Path(root)/"ws_status.json"
    def publish(status):
        status=dict(status,observed_at=time.time(),source="OKX_EEA_WS_SHADOW")
        try:
            save(path,status)
            logging.info("WS shadow: connected=%s ready=%s trades5m=%s book_age=%s",status["connected"],status["transport_ready"],status["trade_count_5m"],status["book_age_seconds"])
        except Exception:
            logging.exception("WS status write failed")
    while True:
        try:
            metadata()
            asyncio.run(run(Feed(),publish))
        except Exception:
            logging.exception("WS shadow worker failed")
            time.sleep(60)
