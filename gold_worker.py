"""Single-writer research worker. Requires an explicitly mounted persistent volume."""
import os, time, logging, fcntl
from pathlib import Path
from gold_collect import run
from gold_early_cycle import cycle
from gold_history import metadata
import threading
from gold_calendar_cache import refresh
from gold_status import publish
from gold_ws_shadow import worker as ws_worker
from gold_web import serve

def main():
    logging.basicConfig(level=logging.INFO)
    root=Path(os.environ["GOLD_STATE_DIR"]).resolve()
    if not root.is_mount(): raise RuntimeError("GOLD_STATE_DIR must be a dedicated mounted volume")
    with (root/"worker.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        def early_loop():
            state={}
            verified=False
            while True:
                started=time.monotonic()
                try:
                    if not verified:
                        metadata(); verified=True
                    cycle(root,state)
                except Exception:
                    logging.exception('early collection failed')
                time.sleep(max(1,15-(time.monotonic()-started)))
        if os.environ.get('GOLD_EARLY_ENABLED','0')=='1':
            threading.Thread(target=early_loop,daemon=True,name='gold-early').start()
        if os.environ.get('GOLD_WS_SHADOW_ENABLED','1')=='1':
            threading.Thread(target=ws_worker,args=(root,),daemon=True,name='gold-ws-shadow').start()
        threading.Thread(target=serve,args=(root,),daemon=True,name='gold-web').start()
        def calendar_loop():
            while True:
                try:
                    refresh(root)
                except Exception:
                    logging.exception('calendar refresh failed')
                time.sleep(300)
        def status_loop():
            while True:
                try:
                    publish(root)
                except Exception:
                    logging.exception('status publish failed')
                time.sleep(30)
        threading.Thread(target=status_loop,daemon=True,name='gold-status').start()
        threading.Thread(target=calendar_loop,daemon=True,name='gold-calendar').start()
        while True:
            try:
                run(root)
                logging.info("collection complete")
            except Exception:
                logging.exception("collection failed")
            now=time.time()
            time.sleep(max(1, (int(now)//300+1)*300+20-now))

if __name__=="__main__":
    main()
