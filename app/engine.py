"""Engine: owns the client, runs the scanner loop, executes orders through risk checks."""
from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from decimal import Decimal

from . import risk
from .config import Config
from .db import DB
from .kalshi_client import KalshiClient, KalshiError, d
from .scanner import run_scan

NEVER_AUTO = {"basket_yes", "wide_spread", "movers"}


class Engine:
    def __init__(self, cfg: Config, db: DB):
        self.cfg, self.db = cfg, db
        self.client: KalshiClient | None = None
        self.series_cache: dict = {}
        self.scan_state = {"running": False, "last": None, "error": None, "stats": {}, "next": None}
        self.positions_cache = {"ts": 0, "data": None}
        self.spend_cache = {"ts": 0, "data": None}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._scan_now = threading.Event()
        from .alerts import Alerter
        from .ws_feed import Feed
        self.alerts = Alerter(self)
        self.db.audit_listeners.append(self.alerts.on_audit)
        self.feed = Feed(self)
        self.feed.listeners.append(self._on_feed)
        self.feed.listeners.append(self.alerts.on_feed)
        self.connect()
        from .quoter import Quoter
        self.quoter = Quoter(self)
        from .clv import CLVTracker
        self.clv = CLVTracker(self)
        self.feed.listeners.append(self.quoter.on_feed)

    # ------------------------------------------------------------ connection --
    def connect(self):
        c = self.cfg
        try:
            self.client = KalshiClient(c.base_url, c.key_id, c.key_path) if c.key_id and c.key_path \
                else KalshiClient(c.base_url)
        except Exception as e:
            self.client = KalshiClient(c.base_url)
            self.db.audit("connect_error", f"could not load {c.env} key: {e}")
        self.series_cache = {}
        if getattr(self, "clv", None):
            self.clv.markets.clear()
        self.positions_cache = {"ts": 0, "data": None}
        self.spend_cache = {"ts": 0, "data": None}
        self.db.audit("connect", f"env={c.env} authenticated={self.client.authenticated}")
        if getattr(self, "feed", None):
            self.feed.restart()

    # ------------------------------------------------------------- live feed --
    def _on_feed(self, kind: str, msg: dict):
        """WebSocket events. Fills are stored and invalidate the position/spend caches."""
        if kind == "fill":
            if self.db.record_fill(self.cfg.env, msg, "ws"):
                self.alerts.on_fill(msg)
            self.positions_cache["ts"] = 0
            self.spend_cache["ts"] = 0
        elif kind == "order":
            if d(msg.get("fill_count_fp")) > 0 or msg.get("status") != "resting":
                self.spend_cache["ts"] = 0
        elif kind == "position":
            self.positions_cache["ts"] = 0
        elif kind == "resync":
            threading.Thread(target=self.sync_fills, daemon=True, name="fill-resync").start()
        elif kind == "feed_down":
            self.db.audit("feed_down", f"WebSocket dropped ({msg.get('error')}); REST polling until reconnect")

    def sync_fills(self, lookback: int = 3 * 86400) -> int:
        """Pull recent fills over REST (after a reconnect, or as the polling fallback)."""
        if not self.client.authenticated:
            return 0
        row = self.db.query("SELECT MAX(ts) t FROM fills WHERE env=?", (self.cfg.env,))
        since = int((row[0]["t"] or 0) - 300) if row and row[0]["t"] else int(time.time()) - lookback
        n = 0
        try:
            for f in self.client.fills(min_ts=max(since, int(time.time()) - lookback), max_items=5000):
                if self.db.record_fill(self.cfg.env, f, "rest"):
                    n += 1
                    self.alerts.on_fill(f)
        except Exception as e:
            self.db.audit("fill_sync_error", str(e))
        if n:
            self.positions_cache["ts"] = 0
            self.spend_cache["ts"] = 0
        return n

    def positions(self, max_age=10):
        if not self.client.authenticated:
            return None
        with self._lock:
            if time.time() - self.positions_cache["ts"] > max_age:
                self.positions_cache = {"ts": time.time(), "data": self.client.positions()}
            return self.positions_cache["data"]

    # ------------------------------------------------------- spend & losses --
    def spent_today(self, max_age=3) -> dict:
        """$ actually FILLED on opening (buy) orders today, from Kalshi's own order records.
        Unfilled and cancelled orders count $0. Uses our logged limit price for our orders
        (an upper bound on the real cost); outside orders use Kalshi's quoted price."""
        if not self.client.authenticated:
            return {"all": Decimal(0), "auto": Decimal(0), "resting_buy": Decimal(0)}
        with self._lock:
            if time.time() - self.spend_cache["ts"] <= max_age:
                return self.spend_cache["data"]
        orders = self.client.orders(status=None, min_ts=int(risk.start_of_today()), max_items=5000)
        ours = {r["order_id"]: r for r in self.db.query(
            "SELECT order_id,intent,price,source FROM orders_log WHERE env=? AND ts>=? AND status='placed'",
            (self.cfg.env, risk.start_of_today() - 3600))}
        s_all = s_auto = resting = Decimal(0)
        for o in orders:
            fc, rem = d(o.get("fill_count_fp")), d(o.get("remaining_count_fp"))
            mine = ours.get(o.get("order_id"))
            if mine:
                if not mine["intent"].startswith("buy"):
                    continue
                px = d(mine["price"])
                auto = mine["source"] in ("auto", "quoter")
            elif o.get("outcome_side") == "yes" and o.get("book_side") == "bid":
                px, auto = d(o.get("yes_price_dollars")), False
            elif o.get("outcome_side") == "no" and o.get("book_side") == "ask":
                px, auto = d(o.get("no_price_dollars")), False
            else:
                continue
            s_all += fc * px
            if auto:
                s_auto += fc * px
            if o.get("status") == "resting":
                resting += rem * px
        data = {"all": s_all, "auto": s_auto, "resting_buy": resting}
        with self._lock:
            self.spend_cache = {"ts": time.time(), "data": data}
        return data

    def equity(self) -> Decimal:
        b = self.client.balance()
        return (d(b.get("balance")) + d(b.get("portfolio_value"))) / 100

    def daily_pnl(self) -> dict | None:
        """Equity change since the first check today. Deposits/withdrawals distort it."""
        if not self.client.authenticated:
            return None
        eq = self.equity()
        day = risk.today_key()
        row = self.db.query("SELECT start_equity FROM day_equity WHERE env=? AND day=?", (self.cfg.env, day))
        if not row:
            self.db.execute("INSERT OR IGNORE INTO day_equity VALUES(?,?,?,?)", (self.cfg.env, day, float(eq), time.time()))
            start = eq
        else:
            start = Decimal(str(row[0]["start_equity"]))
        return {"start": start, "now": eq, "pnl": eq - start}

    def check_loss_limit(self):
        """Trip the kill switch if today's loss reaches the limit."""
        lim = d(self.cfg.risk.get("max_daily_loss") or 0)
        if lim <= 0 or self.cfg.risk.get("kill_switch") or not self.client.authenticated:
            return
        p = self.daily_pnl()
        if p and -p["pnl"] >= lim:
            self.trip_kill(f"Daily loss limit hit: down ${-p['pnl']:.2f} today (limit ${lim:.2f})")

    def trip_kill(self, reason: str) -> list:
        self.cfg.update({"risk": {"kill_switch": True}, "auto": {"enabled": False}, "quoter": {"enabled": False}})
        cancelled = []
        if getattr(self, "quoter", None):
            self.quoter.quotes.clear()
        if self.client.authenticated:
            for o in self.client.orders(status="resting"):
                try:
                    self.client.cancel_order(o["order_id"], o.get("ticker"))
                    cancelled.append(o["order_id"])
                except Exception:
                    pass
        self.db.audit("KILL", f"{reason} | cancelled {len(cancelled)} orders")
        return cancelled

    # --------------------------------------------------------------- orders --
    def place(self, *, ticker: str, intent: str, price, count, source="manual", strategy=None,
              time_in_force="good_till_canceled", post_only=False, expiration_time=None, market=None):
        """intent: buy_yes | buy_no | sell_yes | sell_no. price is in the intent's own terms
        (a NO price for *_no). Returns the log row id and Kalshi response."""
        if intent not in ("buy_yes", "buy_no", "sell_yes", "sell_no"):
            raise risk.RiskError("Unknown intent")
        base = dict(env=self.cfg.env, source=source, strategy=strategy, ticker=ticker, intent=intent,
                    price=str(d(price)), count=str(d(count)))
        try:
            if intent.startswith("buy"):
                self.check_loss_limit()
            market = market or self.client.market(ticker)
            positions = self.positions(max_age=0 if source != "quoter" else 5)
            cat = None
            if self.cfg.risk.get("blocked_categories"):
                try:
                    ev = self.client.request("GET", f"/events/{market['event_ticker']}").get("event") or {}
                    cat = ev.get("category")
                except Exception:
                    pass
            spent = self.spent_today(max_age=0 if source != "quoter" else 5) if intent.startswith("buy") else {}
            cost = risk.check(self.cfg, intent=intent, price=price, count=count, market=market,
                              positions=positions, spent=spent, source=source, category=cat)
            if intent.startswith("sell"):
                held = self._held(positions, ticker)
                want = d(count)
                if intent == "sell_yes" and held < want:
                    raise risk.RiskError(f"You hold {held} YES; can't sell {want} (would open a NO position). "
                                         f"Use Buy NO to open.")
                if intent == "sell_no" and -held < want:
                    raise risk.RiskError(f"You hold {-held} NO; can't sell {want}.")
        except risk.RiskError as e:
            if source != "quoter":  # quoter refusals are routine; they go to the quoter log instead
                self.db.log_order(**base, status="blocked", error=str(e))
            raise
        book_side, yes_price = risk.to_book(intent, price)
        coid = str(uuid.uuid4())
        base.update(book_side=book_side, cost=str(cost), client_order_id=coid)
        try:
            resp = self.client.create_order(ticker, book_side, yes_price, count, time_in_force=time_in_force,
                                            post_only=post_only, expiration_time=expiration_time,
                                            client_order_id=coid)
        except KalshiError as e:
            rid = self.db.log_order(**base, status="rejected", error=str(e), raw=e.payload)
            raise
        rid = self.db.log_order(**base, status="placed", order_id=resp.get("order_id"),
                                fill_count=resp.get("fill_count"), avg_fill_price=resp.get("average_fill_price"),
                                avg_fee=resp.get("average_fee_paid"), raw=resp)
        self.positions_cache["ts"] = 0
        self.spend_cache["ts"] = 0
        return {"log_id": rid, "kalshi": resp, "book_side": book_side, "yes_price": str(yes_price)}

    def fit_size(self, legs, source="approved", spent=None) -> int:
        """Largest per-leg contract count that fits the per-order and remaining daily limits."""
        r = self.cfg.risk
        spent = spent if spent is not None else self.spent_today(max_age=30)
        per_order = d(r["max_order_cost"])
        daily_left = d(r["max_daily_spend"]) - d(spent["all"])
        if source == "auto":
            per_order = min(per_order, d(self.cfg.auto["max_order_cost"]))
            daily_left = min(daily_left, d(self.cfg.auto["max_daily_spend"]) - d(spent["auto"]))
        prices = [d(l["price"]) for l in legs if l["intent"].startswith("buy")]
        if not prices:
            return int(d(r["max_contracts_per_order"]))
        n = min(per_order / max(prices), daily_left / sum(prices), d(r["max_contracts_per_order"]))
        return max(0, int(n))

    def _preflight_basket(self, legs, cnt, source):
        """Run every leg's risk check AND the combined cost against daily limits before
        placing anything, so a basket can't be stopped halfway by our own limits."""
        positions = self.positions(max_age=0)
        sp = self.spent_today(max_age=0)
        total = Decimal(0)
        for leg in legs:
            m = self.client.market(leg["ticker"])
            total += risk.check(self.cfg, intent=leg["intent"], price=leg["price"], count=cnt,
                                market=m, positions=positions, spent=sp, source=source)
        if sp["all"] + total > d(self.cfg.risk["max_daily_spend"]):
            raise risk.RiskError(f"Basket ${total:.2f} + ${sp['all']:.2f} filled today > daily limit")
        if source == "auto" and sp["auto"] + total > d(self.cfg.auto["max_daily_spend"]):
            raise risk.RiskError("Basket exceeds auto daily limit")
        tot_exp = sum((abs(d(mp.get("market_exposure_dollars"))) for mp in (positions or {}).get("market_positions", [])),
                      Decimal(0))
        if positions is not None and tot_exp + total > d(self.cfg.risk["max_total_exposure"]):
            raise risk.RiskError("Basket exceeds total exposure limit")

    @staticmethod
    def _held(positions, ticker) -> Decimal:
        for mp in (positions or {}).get("market_positions", []):
            if mp.get("ticker") == ticker:
                return d(mp.get("position_fp"))
        return Decimal(0)

    def execute_signal(self, sig: dict, count: int | None = None, source="approved"):
        """count = contracts per leg (= number of baskets for multi-leg signals).
        Defaults to the depth-based size the scanner suggested."""
        legs = json.loads(sig["legs"]) if isinstance(sig["legs"], str) else sig["legs"]
        if not legs:
            raise risk.RiskError("Signal has no legs to trade")
        results = []
        multi = len(legs) > 1
        sizes = [max(1, int(leg.get("count") or 1)) if not count else max(1, int(count)) for leg in legs]
        if multi:
            sizes = [min(sizes)] * len(legs)  # baskets must be equal-sized
        if source == "auto":
            cap = self.fit_size(legs, source)
            if cap < 1:
                raise risk.RiskError("Auto limits too small for even 1 contract/basket")
            sizes = [min(s, cap) for s in sizes]
        if multi:
            self._preflight_basket(legs, sizes[0], source)
        for leg, cnt in zip(legs, sizes):
            try:
                r = self.place(ticker=leg["ticker"], intent=leg["intent"], price=leg["price"], count=cnt,
                               source=source, strategy=sig["strategy"],
                               time_in_force="fill_or_kill" if multi else "good_till_canceled")
                results.append({"ticker": leg["ticker"], "ok": True, "resp": r})
                if multi and d(r["kalshi"].get("fill_count")) <= 0:
                    results[-1]["ok"] = False
                    break
            except Exception as e:
                results.append({"ticker": leg["ticker"], "ok": False, "error": str(e)})
                break
        done = all(r["ok"] for r in results) and len(results) == len(legs)
        status = "executed" if done else ("partial" if any(r["ok"] for r in results) else "failed")
        self.db.execute("UPDATE signals SET status=?, note=? WHERE id=?",
                        (status, json.dumps(results, default=str)[:4000], sig["id"]))
        if status == "partial":
            self.db.audit("LEG_RISK", f"Signal {sig['id']} partially executed - check positions NOW")
        return {"status": status, "results": results}

    # -------------------------------------------------------------- scanner --
    def scan_once(self):
        self.scan_state["running"] = True
        try:
            res = run_scan(self.client, self.cfg, self.db, self.series_cache)
            env = self.cfg.env
            self.db.execute("DELETE FROM signals WHERE env=? AND status='new'", (env,))
            now = time.time()
            for s in res["signals"]:
                self.db.execute(
                    "INSERT OR REPLACE INTO signals(ts,env,strategy,key,title,detail,edge,actionable,legs,status,note)"
                    " VALUES(?,?,?,?,?,?,?,?,?, 'new', ?)",
                    (now, env, s["strategy"], s["key"], s["title"], s["detail"], s["edge"],
                     int(s["actionable"]), json.dumps(s["legs"]), s.get("warning", "")))
            self.scan_state.update(last=now, error=None, stats={k: res[k] for k in ("events", "markets", "secs")}
                                   | {"signals": len(res["signals"])})
            self._auto_execute()
        except Exception as e:
            self.scan_state["error"] = f"{e}"
            self.db.audit("scan_error", traceback.format_exc()[-1500:])
        finally:
            self.scan_state["running"] = False

    def _auto_execute(self):
        a = self.cfg.auto
        if not a.get("enabled") or self.cfg.risk.get("kill_switch") or not self.client.authenticated:
            return
        allowed = set(a.get("strategies") or []) - NEVER_AUTO
        for sig in self.db.query("SELECT * FROM signals WHERE env=? AND status='new' AND actionable=1",
                                 (self.cfg.env,)):
            if sig["strategy"] in allowed:
                try:
                    r = self.execute_signal(sig, None, source="auto")
                    self.db.audit("auto", f"{sig['key']} -> {r['status']}")
                except Exception as e:
                    self.db.audit("auto_error", f"{sig['key']}: {e}")

    def loop(self):
        while not self._stop.is_set():
            try:
                self.check_loss_limit()
            except Exception as e:
                self.db.audit("loss_check_error", str(e))
            self.scan_once()
            try:
                pos = self.positions(max_age=30) or {}
                self.feed.want("positions", [mp["ticker"] for mp in pos.get("market_positions", [])])
                if not self.feed.live():
                    self.sync_fills()
            except Exception as e:
                self.db.audit("feed_watch_error", str(e))
            wait = max(30, int(self.cfg.scanner["interval_sec"]))
            self.scan_state["next"] = time.time() + wait
            self._scan_now.wait(wait)
            self._scan_now.clear()

    def trigger_scan(self):
        self._scan_now.set()

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="scanner").start()
        if self.cfg.data.get("ws_enabled", True):
            self.feed.start()
        self.quoter.start()
        self.alerts.start()
        self.clv.start()

    def stop(self, reason: str = "clean shutdown"):
        self._stop.set()
        self._scan_now.set()
        self.quoter.stop()
        self.feed.stop()
        self.clv.stop()
        self.alerts.stop(reason)
