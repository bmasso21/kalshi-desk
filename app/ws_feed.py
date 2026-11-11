"""Real-time data from Kalshi's WebSocket API (Trade API ws/v2).

What it does
  * One authenticated connection (signed handshake: timestamp + "GET" + "/trade-api/ws/v2").
  * Account channels for ALL markets: user_orders (order state), fill, market_positions.
  * Market channels for a dynamic watch set: orderbook_delta (snapshot + deltas) and ticker.
    Owners declare what they need (quoter -> its markets, positions -> held markets,
    ui -> markets viewed in the last 5 min) and the feed adds/removes markets on the live
    subscription with update_subscription add_markets / delete_markets.
  * Every orderbook delta is applied in seq order. A seq gap marks the books unsynced and
    requests a fresh snapshot (update_subscription get_snapshot).
  * Auto-reconnect with exponential backoff (1s -> 30s). After a reconnect the account state
    (resting orders, recent fills) is re-seeded over REST so nothing is missed.

Fallback
  Consumers ask the feed first and fall back to REST polling when it returns None:
    feed.top(ticker), feed.book(ticker)  -> None unless connected AND that book is synced
    feed.resting_orders()                -> None unless connected AND order state is seeded
  So if the feed drops, the app keeps working exactly as v1.1 did (REST polling).
"""
from __future__ import annotations

import asyncio
import collections
import json
import threading
import time
import traceback
from decimal import Decimal
from typing import Callable
from urllib.parse import urlparse

from .kalshi_client import d, sign

ONE = Decimal(1)
WS_URLS = {
    "demo": "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
    "prod": "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
}
MAX_MARKETS = 250            # cap on watched markets (orderbook + ticker)
UI_TTL = 300                 # a market opened in the UI stays watched this long
ACCOUNT_CHANNELS = ("user_orders", "fill", "market_positions")


class Feed:
    def __init__(self, engine):
        self.e = engine
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws = None
        self._stop = threading.Event()
        self._gen = 0                       # bumps on restart; stale loops exit
        self.listeners: list[Callable[[str, dict], None]] = []
        self.events = collections.deque(maxlen=200)
        self._reset_session()
        self.owners: dict[str, set] = {}    # owner -> tickers
        self.ui_watch: dict[str, float] = {}  # ticker -> expiry
        self.stats = {"msgs": 0, "deltas": 0, "snapshots": 0, "gaps": 0, "reconnects": 0, "fills": 0,
                      "order_updates": 0, "errors": 0}
        self._had_conn = False
        self.state = {"enabled": False, "connected": False, "since": None, "last_msg": None,
                      "error": None, "url": None, "reason": "not started"}

    # ----------------------------------------------------------- session --
    def _reset_session(self):
        with self._lock:
            self.books: dict[str, dict] = {}     # ticker -> {"yes": {px: qty}, "no": {...}, "synced", "ts", "sid"}
            self.tickers: dict[str, dict] = {}   # ticker -> last ticker msg (+ "_ts")
            self.orders: dict[str, dict] = {}    # order_id -> resting user_order
            self.positions: dict[str, dict] = {}
            self.orders_synced = False
            self.order_seen: dict[str, tuple[float, str]] = {}  # order_id -> (ts, status) from the feed
            self.sids: dict[str, int] = {}       # channel -> sid
            self.sid_channel: dict[int, str] = {}
            self.seq: dict[int, int] = {}
            self.subscribed: set[str] = set()    # markets on the orderbook/ticker subscriptions
            self.pending: dict[int, dict] = {}   # cmd id -> {"channel"/"action", ...}
            self._cmd = 0

    def log(self, kind, msg):
        self.events.appendleft({"ts": time.time(), "kind": kind, "msg": str(msg)[:500]})

    # -------------------------------------------------------- public API --
    def url(self) -> str:
        c = self.e.cfg
        o = (c.data.get("ws_url_override") or "").strip()
        return o or WS_URLS[c.env]

    def live(self) -> bool:
        return bool(self.state["connected"])

    def top(self, ticker: str) -> dict | None:
        """Top of book from the live feed, or None (caller falls back to REST)."""
        if not self.live():
            return None
        with self._lock:
            b = self.books.get(ticker)
            if not b or not b["synced"]:
                return None
            yb = max(b["yes"]) if b["yes"] else Decimal(0)
            nb = max(b["no"]) if b["no"] else Decimal(0)
            return {"yes_bid": yb, "yes_ask": (ONE - nb) if nb else Decimal(0),
                    "no_bid": nb, "no_ask": (ONE - yb) if yb else Decimal(0),
                    "yes_bid_size": b["yes"].get(yb, Decimal(0)), "no_bid_size": b["no"].get(nb, Decimal(0)),
                    "ts": b["ts"]}

    def book(self, ticker: str, depth: int = 10) -> dict | None:
        """Same shape as REST orderbook_fp: {"yes_dollars": [[px, qty]...], "no_dollars": [...]}."""
        if not self.live():
            return None
        with self._lock:
            b = self.books.get(ticker)
            if not b or not b["synced"]:
                return None
            f = lambda side: [[f"{p:.4f}", f"{q:.2f}"] for p, q in sorted(b[side].items(), key=lambda x: -x[0])[:depth]]
            return {"yes_dollars": f("yes"), "no_dollars": f("no")}

    def market_overlay(self, m: dict) -> dict:
        """Return a copy of a REST Market dict with live top-of-book prices when available."""
        t = self.top(m.get("ticker", ""))
        if not t or t["yes_bid"] <= 0 or t["yes_ask"] <= 0:
            return m
        return {**m, "yes_bid_dollars": f"{t['yes_bid']:.4f}", "yes_ask_dollars": f"{t['yes_ask']:.4f}",
                "no_bid_dollars": f"{t['no_bid']:.4f}", "no_ask_dollars": f"{t['no_ask']:.4f}", "_live": True}

    def resting_orders(self) -> list | None:
        if not self.live():
            return None
        with self._lock:
            if not self.orders_synced:
                return None
            return [dict(o) for o in self.orders.values()]

    def want(self, owner: str, tickers) -> None:
        with self._lock:
            self.owners[owner] = {t for t in tickers if t}
        self._kick()

    def want_ui(self, ticker: str) -> None:
        with self._lock:
            self.ui_watch[ticker] = time.time() + UI_TTL
        self._kick()

    def watch_set(self) -> set:
        now = time.time()
        with self._lock:
            for t in [t for t, x in self.ui_watch.items() if x < now]:
                del self.ui_watch[t]
            # priority: quoter, positions, then other owners, then UI
            order = ["quoter", "positions"] + sorted(k for k in self.owners if k not in ("quoter", "positions"))
            out: list[str] = []
            for k in order:
                out += sorted(self.owners.get(k, ()))
            out += sorted(self.ui_watch, key=lambda t: -self.ui_watch[t])
            seen, res = set(), []
            for t in out:
                if t not in seen:
                    seen.add(t)
                    res.append(t)
            return set(res[:MAX_MARKETS])

    def status(self) -> dict:
        with self._lock:
            synced = sum(1 for b in self.books.values() if b["synced"])
            return {**self.state, "mode": "websocket" if self.live() else "polling",
                    "markets_watched": len(self.subscribed), "books_synced": synced,
                    "orders_synced": self.orders_synced, "resting_orders": len(self.orders),
                    "stats": dict(self.stats), "events": list(self.events)[:30]}

    # --------------------------------------------------------- lifecycle --
    def start(self):
        self._stop.clear()
        self._gen += 1
        gen = self._gen
        self._thread = threading.Thread(target=self._run, args=(gen,), daemon=True, name=f"ws-feed-{gen}")
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._gen += 1
        self._close_ws()
        self.state.update(connected=False, reason="stopped")

    def restart(self):
        """Called when env or keys change."""
        if self._thread is None:
            return
        self.stop()
        self._reset_session()
        self.start()

    def _close_ws(self):
        loop, ws = self._loop, self._ws
        if loop and ws and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(ws.close(), loop)
            except Exception:
                pass

    def _kick(self):
        ev = getattr(self, "_wake", None)
        loop = self._loop
        if ev is not None and loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(ev.set)
            except RuntimeError:
                pass

    def _run(self, gen: int):
        try:
            asyncio.run(self._main(gen))
        except Exception:
            if gen == self._gen:
                self.state.update(connected=False, error=traceback.format_exc()[-500:])

    def _current(self, gen) -> bool:
        return gen == self._gen and not self._stop.is_set()

    async def _main(self, gen: int):
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        backoff = 1.0
        while self._current(gen):
            client = self.e.client
            if not client or not client.authenticated:
                self.state.update(enabled=False, connected=False, reason="no API key - using REST polling")
                await self._sleep(5, gen)
                continue
            self.state.update(enabled=True, url=self.url())
            started = time.time()
            self._had_conn = False
            try:
                await self._session(client, gen)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not self._current(gen):
                    break          # a newer session owns the state now; don't touch it
                self.stats["errors"] += 1
                msg = f"{type(e).__name__}: {e}"
                self.state["error"] = msg[:300]
                self.log("disconnect", msg)
            if not self._current(gen):
                break
            self.state.update(connected=False, reason="reconnecting - using REST polling meanwhile")
            with self._lock:
                self.orders_synced = False
                for b in self.books.values():
                    b["synced"] = False
            if time.time() - started > 60:
                backoff = 1.0
            self.stats["reconnects"] += 1
            if self._had_conn:
                self._notify("feed_down", {"error": self.state["error"], "retry_in": backoff})
            await self._sleep(backoff, gen)
            backoff = min(30.0, backoff * 2)

    async def _sleep(self, secs, gen):
        end = time.time() + secs
        while time.time() < end and not self._stop.is_set() and gen == self._gen:
            await asyncio.sleep(min(0.5, end - time.time()))

    # ----------------------------------------------------------- session --
    async def _session(self, client, gen):
        from websockets.asyncio.client import connect

        url = self.url()
        path = urlparse(url).path or "/trade-api/ws/v2"
        ts = str(int(time.time() * 1000))
        headers = {"KALSHI-ACCESS-KEY": client.key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                   "KALSHI-ACCESS-SIGNATURE": sign(client.private_key, ts + "GET" + path)}
        if not self._current(gen):
            return
        self._reset_session()
        async with connect(url, additional_headers=headers, open_timeout=15, ping_interval=20,
                           ping_timeout=20, close_timeout=5, max_size=2 ** 24, max_queue=4096) as ws:
            if not self._current(gen):
                return
            self._ws = ws
            self._loop = asyncio.get_running_loop()
            self.state.update(connected=True, since=time.time(), error=None, reason="live")
            self._had_conn = True
            self.log("connect", url)
            self._notify("feed_up", {"url": url})
            for ch in ACCOUNT_CHANNELS:
                await self._send(ws, "subscribe", {"channels": [ch]}, channel=ch)
            # seed resting orders over REST after subscribing, so no update is missed
            asyncio.get_running_loop().run_in_executor(None, self._seed_account)
            reader = asyncio.create_task(self._reader(ws, gen))
            try:
                while not reader.done():
                    if self._stop.is_set() or gen != self._gen:
                        await ws.close()
                        break
                    await self._reconcile(ws)
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
                    self._wake.clear()
                if reader.done() and reader.exception():
                    raise reader.exception()
            finally:
                reader.cancel()
                self._ws = None

    async def _send(self, ws, cmd, params=None, **meta):
        with self._lock:
            self._cmd += 1
            cid = self._cmd
            self.pending[cid] = {"cmd": cmd, **meta}
        body = {"id": cid, "cmd": cmd}
        if params is not None:
            body["params"] = params
        await ws.send(json.dumps(body))
        return cid

    async def _reconcile(self, ws):
        """Bring the live market subscriptions in line with the watch set."""
        want = self.watch_set()
        with self._lock:
            have = set(self.subscribed)
            sids = dict(self.sids)
            waiting = any(p.get("channel") in ("orderbook_delta", "ticker") and p["cmd"] == "subscribe"
                          for p in self.pending.values())
        if waiting:
            return
        add, drop = sorted(want - have), sorted(have - want)
        for ch in ("orderbook_delta", "ticker"):
            sid = sids.get(ch)
            if sid is None:
                if want:
                    await self._send(ws, "subscribe", {"channels": [ch], "market_tickers": sorted(want)},
                                     channel=ch, markets=sorted(want))
                continue
            if add:
                await self._send(ws, "update_subscription", {"sid": sid, "action": "add_markets",
                                                             "market_tickers": add}, action="add", channel=ch)
            if drop:
                await self._send(ws, "update_subscription", {"sid": sid, "action": "delete_markets",
                                                             "market_tickers": drop}, action="drop", channel=ch)
        with self._lock:
            if sids.get("orderbook_delta") is None and want:
                self.subscribed = set(want)
            else:
                self.subscribed = (self.subscribed | set(add)) - set(drop)
            for t in drop:
                self.books.pop(t, None)
                self.tickers.pop(t, None)
            for t in add if sids.get("orderbook_delta") is not None else want:
                self.books.setdefault(t, {"yes": {}, "no": {}, "synced": False, "ts": 0, "asked": time.time()})
        # books that never got a snapshot (e.g. add_markets without one) -> ask explicitly
        sid = sids.get("orderbook_delta")
        if sid is not None:
            now = time.time()
            with self._lock:
                stale = [t for t, b in self.books.items() if not b["synced"] and now - b.get("asked", 0) > 5]
                for t in stale:
                    self.books[t]["asked"] = now
            if stale:
                await self._send(ws, "update_subscription", {"sid": sid, "action": "get_snapshot",
                                                             "market_tickers": stale}, action="snapshot")

    async def _reader(self, ws, gen):
        async for raw in ws:
            if gen != self._gen:
                return
            self.state["last_msg"] = time.time()
            self.stats["msgs"] += 1
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            try:
                await self._handle(ws, m)
            except Exception:
                self.stats["errors"] += 1
                self.log("handler_error", traceback.format_exc()[-400:])

    # ---------------------------------------------------------- messages --
    async def _handle(self, ws, m: dict):
        typ, msg, sid = m.get("type"), m.get("msg") or {}, m.get("sid")
        if typ == "subscribed":
            ch, s = msg.get("channel"), msg.get("sid", sid)
            with self._lock:
                p = self.pending.pop(m.get("id"), None) or {}
                ch = ch or p.get("channel")
                self.sids[ch] = s
                self.sid_channel[s] = ch
            self.log("subscribed", f"{ch} sid={s}")
            if ch in ("orderbook_delta", "ticker"):
                self._kick()
            return
        if typ in ("ok", "unsubscribed"):
            with self._lock:
                self.pending.pop(m.get("id"), None)
            return
        if typ == "error":
            self.stats["errors"] += 1
            with self._lock:
                p = self.pending.pop(m.get("id"), None) or {}
            self.log("error", f"{msg.get('code')}: {msg.get('msg')} {p}")
            self.state["error"] = f"{msg.get('code')}: {msg.get('msg')}"
            if p.get("cmd") == "subscribe" and p.get("channel") in ("orderbook_delta", "ticker"):
                with self._lock:
                    self.subscribed.clear()
            return

        # sequence check (per subscription)
        if sid is not None and m.get("seq") is not None:
            seq = int(m["seq"])
            with self._lock:
                last = self.seq.get(sid)
                self.seq[sid] = seq
            if typ != "orderbook_snapshot" and last is not None and seq != last + 1:
                self.stats["gaps"] += 1
                ch = self.sid_channel.get(sid)
                self.log("gap", f"{ch} sid={sid} expected {last + 1} got {seq}")
                if ch == "orderbook_delta":
                    with self._lock:
                        tickers = sorted(self.books)
                        for b in self.books.values():
                            b["synced"] = False
                            b["asked"] = time.time()
                    if tickers:
                        await self._send(ws, "update_subscription", {"sid": sid, "action": "get_snapshot",
                                                                     "market_tickers": tickers}, action="snapshot")
                    return
                if ch in ACCOUNT_CHANNELS:
                    asyncio.get_running_loop().run_in_executor(None, self._seed_account)

        if typ == "orderbook_snapshot":
            t = msg.get("market_ticker")
            with self._lock:
                if t not in self.subscribed and t not in self.books:
                    return
                self.books[t] = {"yes": self._levels(msg.get("yes_dollars_fp")),
                                 "no": self._levels(msg.get("no_dollars_fp")),
                                 "synced": True, "ts": time.time(), "clock": msg.get("clock")}
            self.stats["snapshots"] += 1
            self._notify("book", {"ticker": t, "snapshot": True})
        elif typ == "orderbook_delta":
            t = msg.get("market_ticker")
            side = msg.get("side")
            with self._lock:
                b = self.books.get(t)
                if not b or not b["synced"] or side not in ("yes", "no"):
                    return
                p, q = d(msg.get("price_dollars")), d(msg.get("delta_fp"))
                lv = b[side]
                n = lv.get(p, Decimal(0)) + q
                if n > 0:
                    lv[p] = n
                else:
                    lv.pop(p, None)
                b["ts"] = time.time()
                b["clock"] = msg.get("clock", b.get("clock"))
            self.stats["deltas"] += 1
            self._notify("book", {"ticker": t, "side": side, "price": str(p), "delta": str(q),
                                  "client_order_id": msg.get("client_order_id")})
        elif typ == "ticker":
            t = msg.get("market_ticker")
            with self._lock:
                self.tickers[t] = {**msg, "_ts": time.time()}
        elif typ == "user_order":
            oid = msg.get("order_id")
            with self._lock:
                self.order_seen[oid] = (time.time(), msg.get("status"))
                if len(self.order_seen) > 20000:
                    for k in sorted(self.order_seen, key=lambda k: self.order_seen[k][0])[:10000]:
                        del self.order_seen[k]
                if msg.get("status") == "resting":
                    self.orders[oid] = msg
                else:
                    self.orders.pop(oid, None)
            self.stats["order_updates"] += 1
            self._notify("order", msg)
        elif typ == "fill":
            self.stats["fills"] += 1
            self._notify("fill", msg)
        elif typ == "market_position":
            with self._lock:
                self.positions[msg.get("market_ticker")] = msg
            self._notify("position", msg)

    @staticmethod
    def _levels(arr) -> dict:
        out = {}
        for p, q in arr or []:
            q = d(q)
            if q > 0:
                out[d(p)] = q
        return out

    def _notify(self, kind, payload):
        for fn in list(self.listeners):
            try:
                fn(kind, payload)
            except Exception:
                self.log("listener_error", traceback.format_exc()[-300:])

    # --------------------------------------------------- REST re-seeding --
    def _seed_account(self):
        """Resting orders over REST after (re)subscribing. Feed updates that arrive while the
        REST call is in flight win over the REST snapshot, so nothing is lost either way."""
        t0 = time.time()
        try:
            orders = self.e.client.orders(status="resting", max_items=5000)
            with self._lock:
                merged = {o["order_id"]: o for o in orders}
                for oid, (ts, st) in self.order_seen.items():
                    if ts >= t0:
                        if st == "resting" and oid in self.orders:
                            merged[oid] = self.orders[oid]
                        else:
                            merged.pop(oid, None)
                self.orders = merged
                self.orders_synced = True
            self.log("seed", f"{len(merged)} resting orders")
            self._notify("resync", {"orders": len(merged)})
        except Exception as e:
            self.log("seed_error", e)
