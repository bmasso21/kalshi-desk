"""Phase 3: drawdown circuit breakers.

Every `check_min` minutes (current environment, needs a working key):

  Per strategy - P&L of that strategy's fills, each marked to the market: settled markets at their result
  ($1/$0), open markets at the mid (YES bid/ask midpoint, else last price):
      fill P&L = sign x (mark - YES price) x contracts - fee      sign = +1 long YES (buy YES / sell NO), -1 short
  over today (since local midnight) and the last 7 days. Liquidity rewards are NOT counted (Kalshi credits them
  later), so this is the conservative side. Positions opened before the window are not included.
  Strategy = the strategy the app placed the order under (quoter = "liquidity"); "manual" / "outside" fills are
  reported but never trip anything.
    loss today  >= strategy_daily_loss  -> breaker trips for that strategy
    loss 7 days >= strategy_7d_loss     -> breaker trips for that strategy
  A tripped breaker turns the strategy off (quoter: disabled and its quotes cancelled; auto strategy: removed from
  auto-trading), sends a phone alert, and stays tripped until you reset it on the Settings page. While tripped, the
  strategy can't be switched back on.

  Account drawdown - equity (cash + positions, Kalshi's numbers) vs its highest value recorded in the last
  `drawdown_days` days. Drop >= account_drawdown -> the kill switch trips (all orders blocked and cancelled).
  Deposits and withdrawals are taken out of the comparison (Kalshi's transfer history), so they are neither profit nor loss.

Every limit is 0 (= off) until you set it.
"""
from __future__ import annotations

import threading
import time
import traceback

from . import risk
from .clv import clv_sign
from .kalshi_client import d

SCHEMA = """
CREATE TABLE IF NOT EXISTS breaker_equity (env TEXT, ts REAL, equity REAL);
CREATE INDEX IF NOT EXISTS breaker_equity_ts ON breaker_equity(env, ts);
CREATE TABLE IF NOT EXISTS breaker_equity2 (env TEXT, ts REAL, equity REAL, adj REAL);
CREATE INDEX IF NOT EXISTS breaker_equity2_ts ON breaker_equity2(env, ts);
"""
ACTIONABLE_EXCLUDE = {"manual", "outside", "approved"}


def strategy_key(f: dict) -> str:
    if f.get("strategy"):
        return f["strategy"]
    src = f.get("order_source") or ""
    if src in ("manual", "approved"):
        return "manual"
    return src or "outside"


def fill_pnl(f: dict, mark: float | None) -> float | None:
    if mark is None or f.get("yes_price") in (None, ""):
        return None
    c = float(f.get("count") or 0)
    fee = float(f.get("fee") or 0)
    s = clv_sign(f.get("action"), f.get("outcome_side"), f.get("book_side"))
    return s * (mark - float(f["yes_price"])) * c - fee


def market_mark(m: dict | None) -> float | None:
    if not m:
        return None
    res = (m.get("result") or "").lower()
    if res in ("yes", "no"):
        return 1.0 if res == "yes" else 0.0
    yb, ya = float(d(m.get("yes_bid_dollars"))), float(d(m.get("yes_ask_dollars")))
    if 0 < yb < 1 and 0 < ya < 1 and ya >= yb:
        return (yb + ya) / 2
    lp = float(d(m.get("last_price_dollars")))
    return lp if 0 < lp < 1 else None


class Breakers:
    def __init__(self, engine):
        self.e = engine
        self.db = engine.db
        with self.db.lock:
            self.db.conn.executescript(SCHEMA)
        self.state = {"last": None, "error": None, "strategies": {}, "account": None}
        self._stop = threading.Event()
        self.lock = threading.RLock()

    @property
    def cfg(self) -> dict:
        return self.e.cfg.data.setdefault("breakers", {})

    def tripped(self) -> dict:
        return dict(self.cfg.get("tripped") or {})

    def is_tripped(self, strategy: str) -> bool:
        return strategy in (self.cfg.get("tripped") or {})

    # ------------------------------------------------------------- P&L --
    def _marks(self, tickers) -> dict[str, float | None]:
        out, tickers = {}, sorted(set(tickers))
        for i in range(0, len(tickers), 100):
            for m in self.e.client.markets(status=None, tickers=tickers[i:i + 100], exclude_combos=False):
                out[m["ticker"]] = market_mark(m)
        return out

    def strategy_pnl(self, now: float | None = None) -> dict:
        now = now or time.time()
        env = self.e.cfg.env
        day0, week0 = risk.start_of_today(), now - 7 * 86400
        fills = self.db.query("SELECT ticker, ts, action, outcome_side, book_side, yes_price, count, fee, strategy, order_source "
                              "FROM fills WHERE env=? AND ts>=? ORDER BY ts", (env, min(day0, week0)))
        marks = self._marks(f["ticker"] for f in fills) if fills else {}
        out: dict[str, dict] = {}
        for f in fills:
            k = strategy_key(f)
            x = out.setdefault(k, {"today": 0.0, "week": 0.0, "fills_today": 0, "fills_week": 0, "unpriced": 0})
            p = fill_pnl(f, marks.get(f["ticker"]))
            if p is None:
                x["unpriced"] += 1
                continue
            if f["ts"] >= week0:
                x["week"] += p
                x["fills_week"] += 1
            if f["ts"] >= day0:
                x["today"] += p
                x["fills_today"] += 1
        for x in out.values():
            x["today"], x["week"] = round(x["today"], 2), round(x["week"], 2)
        return out

    # ----------------------------------------------------------- checks --
    def check(self, now: float | None = None) -> dict:
        now = now or time.time()
        c = self.cfg
        if not self.e.client or not self.e.client.authenticated:
            return self.state
        with self.lock:
            per = self.strategy_pnl(now)
            lim_d, lim_w = float(c.get("strategy_daily_loss") or 0), float(c.get("strategy_7d_loss") or 0)
            for k, x in per.items():
                if k in ACTIONABLE_EXCLUDE:
                    continue
                if lim_d > 0 and -x["today"] >= lim_d:
                    self.trip(k, f"down ${-x['today']:.2f} today on its fills (limit ${lim_d:.2f})")
                elif lim_w > 0 and -x["week"] >= lim_w:
                    self.trip(k, f"down ${-x['week']:.2f} over 7 days on its fills (limit ${lim_w:.2f})")
            acct = self.account(now)
            self.state.update(last=now, error=None, strategies=per, account=acct)
            return self.state

    def account(self, now: float) -> dict:
        env, c = self.e.cfg.env, self.cfg
        eq = float(self.e.equity())
        moved = self.e.transfers.net(0.0) if getattr(self.e, "transfers", None) else 0.0
        adj = eq - moved                    # equity with every deposit/withdrawal taken out = trading result only
        self.db.execute("INSERT INTO breaker_equity2 VALUES(?,?,?,?)", (env, now, eq, adj))
        since = max(now - float(c.get("drawdown_days") or 30) * 86400, float(c.get("peak_reset_ts") or 0))
        row = self.db.query("SELECT MAX(adj) m FROM breaker_equity2 WHERE env=? AND ts>=?", (env, since))
        peak_adj = max(adj, float(row[0]["m"] if row[0]["m"] is not None else adj))
        dd = peak_adj - adj
        lim = float(c.get("account_drawdown") or 0)
        out = {"equity": round(eq, 2), "peak": round(peak_adj + moved, 2), "drawdown": round(dd, 2), "limit": lim,
               "net_transfers": round(moved, 2)}
        if lim > 0 and dd >= lim and not self.e.cfg.risk.get("kill_switch"):
            self.e.trip_kill(f"Account drawdown breaker: equity ${eq:.2f} is ${dd:.2f} below its "
                             f"{int(c.get('drawdown_days') or 30)}-day peak (limit ${lim:.2f}; deposits/withdrawals excluded)")
            out["tripped"] = True
        return out

    def trip(self, strategy: str, reason: str):
        if self.is_tripped(strategy):
            return
        tr = self.tripped()
        tr[strategy] = {"ts": time.time(), "reason": reason}
        patch: dict = {"breakers": {"tripped": tr}}
        if strategy == "liquidity":
            patch["quoter"] = {"enabled": False}
        auto = list(self.e.cfg.auto.get("strategies") or [])
        if strategy in auto:
            patch["auto"] = {"strategies": [s for s in auto if s != strategy]}
        self.e.cfg.update(patch)
        if strategy == "liquidity" and getattr(self.e, "quoter", None):
            try:
                self.e.quoter.cancel_all(f"circuit breaker: {reason}")
            except Exception:
                pass
        self.db.audit("BREAKER", f"{strategy} turned off - {reason}. Reset it in Settings to re-enable.")

    def reset(self, strategy: str | None = None, peak: bool = False):
        tr = self.tripped()
        if strategy:
            tr.pop(strategy, None)
        patch: dict = {"breakers": {"tripped": tr}}
        if peak:
            patch["breakers"]["peak_reset_ts"] = time.time()
        self.e.cfg.data.setdefault("breakers", {})["tripped"] = tr     # _merge can't delete keys
        self.e.cfg.update(patch)
        self.db.audit("breaker_reset", f"{strategy or ''}{' + account peak' if peak else ''}")

    # ------------------------------------------------------------- loop --
    def loop(self):
        while not self._stop.is_set():
            try:
                if self.cfg.get("enabled", True):
                    self.check()
            except Exception as e:
                self.state["error"] = str(e)
                self.db.audit("breaker_error", traceback.format_exc()[-600:])
            self._stop.wait(max(60, float(self.cfg.get("check_min") or 5) * 60))

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="breakers").start()

    def stop(self):
        self._stop.set()

    def snapshot(self) -> dict:
        c = self.cfg
        return {"state": self.state, "tripped": self.tripped(),
                "limits": {k: c.get(k) for k in ("strategy_daily_loss", "strategy_7d_loss", "account_drawdown",
                                                  "drawdown_days", "check_min", "enabled")}}
