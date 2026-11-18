"""Move the data warehouse (warehouse.db) to another folder or drive, once, at startup.

Triggered by a file named move-data.request next to run.py whose text is the target folder (e.g. D:\\KalshiDesk-data).
Runs BEFORE the app opens the database, so nothing is writing to it. While it runs, a tiny placeholder answers the
app's port so the watchdog does not "restart" a server that is busy copying.

Steps (any failure leaves everything exactly as it was and writes the reason to logs/move-data.txt):
  1. fold the write-ahead log into the main file (so one file holds everything)
  2. check the target drive has room (file size + 10%)
  3. copy to <target>\\warehouse.db.partial, then verify: same size, readable, same page count, same last row ids
  4. rename to warehouse.db, record warehouse_dir in config.json
  5. only then delete the old copy on the original drive."""
from __future__ import annotations

import http.server
import json
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path

CHECK_TABLES = ("wh_trades", "wh_markets", "wh_candles")


def _sig(path: Path) -> dict:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        out = {"page_count": c.execute("PRAGMA page_count").fetchone()[0], "page_size": c.execute("PRAGMA page_size").fetchone()[0]}
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in CHECK_TABLES:
            if t in names:
                out[t] = c.execute(f"SELECT MAX(rowid) FROM {t}").fetchone()[0]
        return out
    finally:
        c.close()


class _Placeholder(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"token": "", "version": "moving data", "note": "Kalshi Desk is moving its data warehouse; back shortly."}).encode()
        self.send_response(200 if self.path.startswith("/api/session") else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def relocate(root: Path, cfg_path: Path, port: int = 8765, placeholder: bool = True) -> dict:
    root, cfg_path = Path(root), Path(cfg_path)
    req = root / "move-data.request"
    if not req.exists():
        return {"done": False, "reason": "no request"}
    log_path = root / "logs" / "move-data.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(msg):
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    target_dir = Path(req.read_text("utf-8").strip().strip('"'))
    try:
        req.unlink()
    except OSError:
        pass
    cfg = json.loads(cfg_path.read_text("utf-8"))
    src = Path(cfg.get("warehouse_dir") or cfg_path.parent) / "warehouse.db"
    dst = target_dir / "warehouse.db"
    srv = None
    if placeholder:
        try:
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Placeholder)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        except OSError as e:
            log(f"placeholder not started ({e}); continuing")
    try:
        log(f"move requested: {src} -> {dst}")
        if not src.exists():
            return _fail(log, f"no warehouse at {src}")
        if src.resolve() == dst.resolve():
            return _fail(log, "already there")
        if dst.exists():
            return _fail(log, f"{dst} already exists - not overwriting it")
        target_dir.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(src))
        try:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            c.close()
        size = src.stat().st_size
        free = shutil.disk_usage(target_dir).free
        log(f"size {size / 1e9:.2f} GB, free on target {free / 1e9:.2f} GB")
        if free < size * 1.1:
            return _fail(log, f"not enough room on {target_dir} ({free / 1e9:.1f} GB free, need {size * 1.1 / 1e9:.1f} GB)")
        before = _sig(src)
        part = target_dir / "warehouse.db.partial"
        t0 = time.time()
        shutil.copyfile(src, part)
        log(f"copied in {time.time() - t0:.0f} s")
        if part.stat().st_size != size:
            part.unlink()
            return _fail(log, "copy is not the same size - removed it")
        after = _sig(part)
        if after != before:
            part.unlink()
            return _fail(log, f"copy does not match (before {before}, after {after}) - removed it")
        part.rename(dst)
        cfg["warehouse_dir"] = str(target_dir)
        tmp = cfg_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, indent=2), "utf-8")
        os.replace(tmp, cfg_path)
        log(f"verified {after}; config now uses {target_dir}")
        for p in (src, src.with_name(src.name + "-wal"), src.with_name(src.name + "-shm")):
            try:
                if p.exists():
                    p.unlink()
            except OSError as e:
                log(f"could not delete {p} ({e}) - delete it by hand to free the space")
        log(f"done: freed {size / 1e9:.2f} GB on the original drive")
        return {"done": True, "from": str(src), "to": str(dst), "gb": round(size / 1e9, 2)}
    except Exception as e:
        return _fail(log, f"error: {e}")
    finally:
        if srv:
            srv.shutdown()
            srv.server_close()


def _fail(log, why):
    log(f"NOT MOVED: {why}. The app keeps using the original file.")
    return {"done": False, "reason": why}
