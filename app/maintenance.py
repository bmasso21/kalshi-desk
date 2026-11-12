"""Nightly maintenance (v3.4): backups and pruning.

Once a day at `time` (local), into <app folder>/backups/:
  kalshi_desk-YYYYMMDD.db   consistent SQLite backup of the ledger (orders, fills, audit, exit rules, breakers)
  config-YYYYMMDD.json      settings (contains your alert keys; same protection as config.json itself)
  warehouse-YYYYMMDD.db     research data, only if it is smaller than `warehouse_max_gb` (it can be rebuilt from Kalshi)
Keeps the last `keep_days` of each. Prunes paper-test price history and live ticks older than `prune_days`.
Backups are on the same disk: they protect against a corrupted file or a bad change, not a dead drive.
Never touches the keys folder.
"""
from __future__ import annotations

import datetime as dt
import os
import shutil
import sqlite3
import threading
import time
import traceback
from pathlib import Path


class Maintenance:
    def __init__(self, engine, wdb):
        self.e, self.wdb = engine, wdb
        self.dir = engine.cfg.path.parent / "backups"
        self.state = {"last": None, "error": None, "result": None}
        self._stop = threading.Event()
        self._last_day = None

    @property
    def cfg(self) -> dict:
        return self.e.cfg.data.setdefault("maintenance", {})

    @staticmethod
    def _backup_sqlite(src_conn_lock, src_conn, dest: Path):
        tmp = dest.with_suffix(".tmp")
        if tmp.exists():
            tmp.unlink()
        out = sqlite3.connect(str(tmp))
        try:
            with src_conn_lock:
                src_conn.backup(out)
        finally:
            out.close()
        for attempt in range(5):                  # Windows: the old backup can be briefly locked (antivirus, a viewer)
            try:
                os.replace(tmp, dest)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(1.0)

    def _rotate(self, prefix: str, keep: int):
        files = sorted(p for p in self.dir.glob(f"{prefix}-*") if not p.name.endswith(".tmp"))
        for p in files[:-keep] if keep > 0 else []:
            try:
                p.unlink()
            except OSError:
                pass

    def sizes(self) -> dict:
        root = self.e.cfg.path.parent
        out = {}
        for name in ("kalshi_desk.db", "warehouse.db"):
            p = root / name
            out[name] = round(p.stat().st_size / 1e6, 1) if p.exists() else None
        out["backups_mb"] = round(sum(p.stat().st_size for p in self.dir.glob("*") if p.is_file()) / 1e6, 1) if self.dir.exists() else 0
        try:
            out["disk_free_gb"] = round(shutil.disk_usage(str(root)).free / 1e9, 1)
        except OSError:
            out["disk_free_gb"] = None
        return out

    def run(self, now: float | None = None) -> dict:
        now = now or time.time()
        c = self.cfg
        day = dt.datetime.fromtimestamp(now).strftime("%Y%m%d")
        keep = int(c.get("keep_days") or 7)
        self.dir.mkdir(exist_ok=True)
        res: dict = {"day": day, "files": [], "pruned": {}}
        db = self.e.db
        self._backup_sqlite(db.lock, db.conn, self.dir / f"kalshi_desk-{day}.db")
        res["files"].append(f"kalshi_desk-{day}.db")
        cfgp = self.e.cfg.path
        if cfgp.exists():
            shutil.copyfile(cfgp, self.dir / f"config-{day}.json")
            res["files"].append(f"config-{day}.json")
        wpath = Path(self.wdb.path)
        max_gb = float(c.get("warehouse_max_gb") or 0)
        if wpath.exists() and max_gb > 0 and wpath.stat().st_size <= max_gb * 1e9:
            self._backup_sqlite(self.wdb.lock, self.wdb.conn, self.dir / f"warehouse-{day}.db")
            res["files"].append(f"warehouse-{day}.db")
            self._rotate("warehouse", min(keep, 2))
        else:
            res["warehouse_skipped"] = "larger than warehouse_max_gb (it can be rebuilt from Kalshi)" if wpath.exists() else "missing"
        self._rotate("kalshi_desk", keep)
        self._rotate("config", keep)
        # pruning
        cut = now - float(c.get("prune_days") or 30) * 86400
        for table, col in (("mm_mids", "ts"), ("wh_ticks", "ts")):
            try:
                cur = self.wdb.execute(f"DELETE FROM {table} WHERE {col} < ?", (cut,))
                res["pruned"][table] = cur.rowcount
            except sqlite3.OperationalError:
                pass
        try:
            cur = db.execute("DELETE FROM breaker_equity2 WHERE ts < ?", (now - 400 * 86400,))
            res["pruned"]["breaker_equity2"] = cur.rowcount
        except sqlite3.OperationalError:
            pass
        res["sizes"] = self.sizes()
        self.state.update(last=now, error=None, result=res)
        db.audit("maintenance", f"backup {', '.join(res['files'])}; pruned {res['pruned']}; free disk {res['sizes'].get('disk_free_gb')} GB")
        free = res["sizes"].get("disk_free_gb")
        if free is not None and free < 5:
            db.audit("warehouse_error", f"LOW DISK SPACE: only {free} GB free on the Kalshi Desk drive")
        return res

    def due(self, now: float | None = None) -> bool:
        c = self.cfg
        if not c.get("enabled", True):
            return False
        n = dt.datetime.fromtimestamp(now or time.time())
        try:
            hh, mm_ = (int(x) for x in str(c.get("time") or "03:30").split(":"))
        except ValueError:
            hh, mm_ = 3, 30
        day = n.strftime("%Y%m%d")
        if self._last_day == day or (self.dir / f"kalshi_desk-{day}.db").exists():
            self._last_day = day
            return False
        return (n.hour, n.minute) >= (hh, mm_)

    def loop(self):
        while not self._stop.is_set():
            try:
                if self.due():
                    self.run()
            except Exception as e:
                self.state["error"] = str(e)[:300]
                self.e.db.audit("warehouse_error", "maintenance failed: " + traceback.format_exc()[-500:])
                self._last_day = dt.datetime.now().strftime("%Y%m%d")       # don't retry in a loop; try again tomorrow
            self._stop.wait(300)

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="maintenance").start()

    def stop(self):
        self._stop.set()
