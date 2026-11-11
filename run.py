"""Start Kalshi Desk:  python run.py   then open http://127.0.0.1:8765"""
import threading
import webbrowser

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
    uvicorn.run(app, host="127.0.0.1", port=int(cfg.data["port"]), log_level="warning")
