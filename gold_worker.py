"""Single-writer research worker. Requires an explicitly mounted persistent volume."""
import os, time, logging, fcntl
from pathlib import Path
from gold_collect import run
from gold_early_cycle import cycle
from gold_history import metadata
import threading

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
