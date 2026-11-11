"""SQLite ledger: every order attempt, every signal, every approval, fills/settlements snapshot."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .config import ROOT

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, env TEXT, source TEXT, strategy TEXT,
    ticker TEXT, intent TEXT, book_side TEXT, price TEXT, count TEXT, cost TEXT,
    status TEXT, order_id TEXT, client_order_id TEXT, fill_count TEXT,
    avg_fill_price TEXT, avg_fee TEXT, error TEXT, raw TEXT
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, env TEXT, strategy TEXT, key TEXT, title TEXT, detail TEXT,
    edge TEXT, actionable INTEGER, legs TEXT, status TEXT DEFAULT 'new', note TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS signals_key ON signals(env, key, status);
CREATE TABLE IF NOT EXISTS fair_values (
    ticker TEXT PRIMARY KEY, fair_yes TEXT, note TEXT, ts REAL
);
CREATE TABLE IF NOT EXISTS settlements (
    env TEXT, ticker TEXT, event_ticker TEXT, settled_time TEXT, market_result TEXT,
    revenue INTEGER, yes_cost TEXT, no_cost TEXT, fee TEXT, raw TEXT,
    PRIMARY KEY (env, ticker, settled_time)
);
CREATE TABLE IF NOT EXISTS day_equity (
    env TEXT, day TEXT, start_equity REAL, ts REAL, PRIMARY KEY (env, day)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, message TEXT
);
"""


class DB:
    def __init__(self, path: Path | str = ROOT / "kalshi_desk.db"):
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def execute(self, sql: str, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def query(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    # --- helpers -------------------------------------------------------------
    def audit(self, kind: str, message: str):
        self.execute("INSERT INTO audit(ts,kind,message) VALUES(?,?,?)", (time.time(), kind, message))

    def log_order(self, **f) -> int:
        f.setdefault("ts", time.time())
        if "raw" in f and not isinstance(f["raw"], str):
            f["raw"] = json.dumps(f["raw"], default=str)
        cols = ",".join(f)
        cur = self.execute(f"INSERT INTO orders_log({cols}) VALUES({','.join('?' * len(f))})",
                           tuple(str(v) if v is not None and not isinstance(v, (int, float)) else v
                                 for v in f.values()))
        return cur.lastrowid

    def spend_since(self, env: str, since_ts: float, source: str | None = None) -> float:
        sql = "SELECT cost FROM orders_log WHERE env=? AND ts>=? AND status='placed'"
        args = [env, since_ts]
        if source:
            sql += " AND source=?"
            args.append(source)
        return sum(float(r["cost"] or 0) for r in self.query(sql, args))
