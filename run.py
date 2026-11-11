"""Start Kalshi Desk:  python run.py   then open http://127.0.0.1:8765"""
import os
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from app.config import Config
from app.db import DB
from app.server import create_app

if __name__ == "__main__":
    cfg = Config()
    app = create_app(cfg, DB())
    url = f"http://127.0.0.1:{cfg.data['port']}"
    print(f"\n  Kalshi Desk running at {url}   (env: {cfg.env.upper()})\n  Close this window to stop.\n")
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    # Drop a file named restart.request next to run.py to make the app shut down cleanly
    # (cancels quoter orders); the boot task's launcher restarts it 15s later.
    flag = Path(__file__).resolve().parent / "restart.request"

    def _watch_restart():
        while True:
            time.sleep(3)
            if flag.exists():
                try:
                    flag.unlink()
                except OSError:
                    pass
                try:
                    app.state.engine.db.audit("restart", "restart.request found - restarting")
                    app.state.engine.stop("restart requested (update)")
                finally:
                    os._exit(3)

    threading.Thread(target=_watch_restart, daemon=True, name="restart-watch").start()

    # Separate watchdog process (alerts + restarts if this server stops responding).
    def _keep_watchdog():
        from app.watchdog import spawn
        time.sleep(20)
        while True:
            try:
                if spawn():
                    app.state.engine.db.audit("watchdog", "watchdog process started")
            except Exception as e:
                app.state.engine.db.audit("watchdog_error", str(e))
            time.sleep(120)

    if os.environ.get("KALSHI_DESK_NO_WATCHDOG") != "1":
        threading.Thread(target=_keep_watchdog, daemon=True, name="watchdog-keeper").start()
    uvicorn.run(app, host="127.0.0.1", port=int(cfg.data["port"]), log_level="warning")
