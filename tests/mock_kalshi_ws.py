"""Mock of Kalshi's WebSocket API (ws/v2) for offline tests.

* Verifies the handshake signature exactly like REST: timestamp + "GET" + "/trade-api/ws/v2".
* subscribe / update_subscription (add_markets, delete_markets, get_snapshot) / unsubscribe.
* orderbook_snapshot + orderbook_delta with per-subscription contiguous seq.
* user_orders, fill, market_positions account channels (all markets).
* Test hooks: push deltas, skip a seq (gap), push user_order/fill, drop the connection, stop/start.
Books are built from the REST mock's market state so both views agree.
"""
from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from decimal import Decimal

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from websockets.asyncio.server import serve
from websockets.http11 import Response
from websockets.datastructures import Headers

PATH = "/trade-api/ws/v2"


class MockWS:
    def __init__(self, public_keys: dict, rest_state: dict, port: int):
        self.keys, self.S, self.port = public_keys, rest_state, port
        self.loop: asyncio.AbstractEventLoop | None = None
        self.server = None
        self.conns: set = set()
        self.subs: dict = {}            # conn -> {sid: {"channel", "markets": set, "seq": int}}
        self.books: dict = {}           # ticker -> {"yes": {px: qty}, "no": {...}}
        self.next_sid = 0
        self.auth_failures = 0
        self.handshakes = 0
        self.commands: list = []
        self._ready = threading.Event()
        self._thread = None

    # ------------------------------------------------------------ books --
    def _market(self, t):
        for e in self.S["events"]:
            for m in e["markets"]:
                if m["ticker"] == t:
                    return m
        return None

    def book(self, t):
        if t not in self.books:
            m = self._market(t)
            yb, nb = Decimal(m["yes_bid_dollars"]), Decimal(m["no_bid_dollars"])
            self.books[t] = {"yes": {yb - Decimal("0.01"): Decimal(50), yb: Decimal(200)},
                             "no": {nb - Decimal("0.01"): Decimal(40), nb: Decimal(200)}}
        return self.books[t]

    # ------------------------------------------------------------- auth --
    def _verify(self, headers) -> bool:
        kid, ts, sig = headers.get("KALSHI-ACCESS-KEY"), headers.get("KALSHI-ACCESS-TIMESTAMP"), headers.get("KALSHI-ACCESS-SIGNATURE")
        if not (kid and ts and sig) or kid not in self.keys or abs(int(ts) - time.time() * 1000) > 30000:
            return False
        pk, msg = self.keys[kid], (ts + "GET" + PATH).encode()
        try:
            raw = base64.b64decode(sig)
            if isinstance(pk, Ed25519PublicKey):
                pk.verify(raw, msg)
            else:
                pk.verify(raw, msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
            return True
        except Exception:
            return False

    def _process_request(self, conn, request):
        self.handshakes += 1
        if request.path != PATH:
            return Response(404, "Not Found", Headers(), b"bad path")
        if not self._verify(request.headers):
            self.auth_failures += 1
            return Response(401, "Unauthorized", Headers(), b"bad signature")
        return None

    # ---------------------------------------------------------- serving --
    async def _send(self, ws, sid, typ, msg, seq=True):
        subs = self.subs.get(ws, {})
        body = {"type": typ, "sid": sid, "msg": msg}
        if seq and sid in subs:
            subs[sid]["seq"] += 1
            body["seq"] = subs[sid]["seq"]
        await ws.send(json.dumps(body))

    async def _snapshot(self, ws, sid, t):
        b = self.book(t)
        f = lambda side: [[f"{p:.4f}", f"{q:.2f}"] for p, q in sorted(b[side].items())]
        await self._send(ws, sid, "orderbook_snapshot",
                         {"market_ticker": t, "market_id": "id-" + t, "clock": int(time.time() * 1000),
                          "yes_dollars_fp": f("yes"), "no_dollars_fp": f("no")})

    async def _ticker(self, ws, sid, t):
        m = self._market(t)
        await self._send(ws, sid, "ticker", {"market_ticker": t, "market_id": "id-" + t,
                                             "price_dollars": m["last_price_dollars"],
                                             "yes_bid_dollars": m["yes_bid_dollars"], "yes_ask_dollars": m["yes_ask_dollars"],
                                             "volume_fp": "100.00", "open_interest_fp": "1000.00",
                                             "ts_ms": int(time.time() * 1000)}, seq=False)

    async def _handler(self, ws):
        self.conns.add(ws)
        self.subs[ws] = {}
        try:
            async for raw in ws:
                c = json.loads(raw)
                self.commands.append(c)
                cid, cmd, p = c.get("id"), c.get("cmd"), c.get("params") or {}
                if cmd == "subscribe":
                    for ch in p.get("channels") or []:
                        markets = set(p.get("market_tickers") or ([p["market_ticker"]] if p.get("market_ticker") else []))
                        if ch in ("orderbook_delta",) and not markets:
                            await ws.send(json.dumps({"id": cid, "type": "error", "msg": {"code": 14, "msg": "Missing market specification"}}))
                            continue
                        self.next_sid += 1
                        sid = self.next_sid
                        self.subs[ws][sid] = {"channel": ch, "markets": markets, "seq": 0}
                        await ws.send(json.dumps({"id": cid, "type": "subscribed", "msg": {"channel": ch, "sid": sid}}))
                        for t in sorted(markets):
                            if ch == "orderbook_delta":
                                await self._snapshot(ws, sid, t)
                            elif ch == "ticker":
                                await self._ticker(ws, sid, t)
                elif cmd == "update_subscription":
                    sid = p.get("sid")
                    sub = self.subs[ws].get(sid)
                    if not sub:
                        await ws.send(json.dumps({"id": cid, "type": "error", "msg": {"code": 7, "msg": "Unknown subscription ID"}}))
                        continue
                    ts_ = set(p.get("market_tickers") or [])
                    act = p.get("action")
                    if act == "add_markets":
                        sub["markets"] |= ts_
                        await ws.send(json.dumps({"id": cid, "sid": sid, "type": "ok", "msg": {"market_tickers": sorted(ts_)}}))
                        for t in sorted(ts_):
                            if sub["channel"] == "orderbook_delta":
                                await self._snapshot(ws, sid, t)
                    elif act == "delete_markets":
                        sub["markets"] -= ts_
                        await ws.send(json.dumps({"id": cid, "sid": sid, "type": "ok", "msg": {"market_tickers": sorted(ts_)}}))
                    elif act == "get_snapshot":
                        for t in sorted(ts_ & sub["markets"]):
                            await self._snapshot(ws, sid, t)
                elif cmd == "unsubscribe":
                    for sid in p.get("sids") or []:
                        self.subs[ws].pop(sid, None)
                else:
                    await ws.send(json.dumps({"id": cid, "type": "error", "msg": {"code": 5, "msg": "Unknown command"}}))
        except Exception:
            pass
        finally:
            self.conns.discard(ws)
            self.subs.pop(ws, None)

    async def _main(self):
        self.loop = asyncio.get_running_loop()
        self.server = await serve(self._handler, "127.0.0.1", self.port, process_request=self._process_request)
        self._ready.set()
        await self.server.serve_forever()

    def start(self):
        self._ready.clear()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)
        self._thread.start()
        self._ready.wait(10)

    def stop(self):
        async def _s():
            self.server.close()
            for c in list(self.conns):
                await c.close()
        self.call(_s())
        self._thread.join(10)

    def call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(10)

    # ------------------------------------------------------------ hooks --
    def _subs_for(self, channel, ticker=None):
        for ws, subs in list(self.subs.items()):
            for sid, s in subs.items():
                if s["channel"] == channel and (ticker is None or not s["markets"] or ticker in s["markets"]):
                    yield ws, sid

    def delta(self, t, side, price, delta, skip_seq=False):
        b = self.book(t)
        p, q = Decimal(str(price)), Decimal(str(delta))
        n = b[side].get(p, Decimal(0)) + q
        if n > 0:
            b[side][p] = n
        else:
            b[side].pop(p, None)

        async def _d():
            for ws, sid in self._subs_for("orderbook_delta", t):
                if t not in self.subs[ws][sid]["markets"]:
                    continue
                if skip_seq:
                    self.subs[ws][sid]["seq"] += 1   # lose one message -> client sees a gap
                await self._send(ws, sid, "orderbook_delta",
                                 {"market_ticker": t, "market_id": "id-" + t, "clock": int(time.time() * 1000),
                                  "price_dollars": f"{p:.4f}", "delta_fp": f"{q:.2f}", "side": side,
                                  "ts_ms": int(time.time() * 1000)})
        self.call(_d())

    def push(self, channel, typ, msg):
        async def _p():
            for ws, sid in self._subs_for(channel):
                await self._send(ws, sid, typ, msg)
        self.call(_p())

    def order_event(self, o: dict):
        """Bridge from the REST mock: every order create/cancel becomes a user_order message."""
        if self.loop is None:
            return
        self.push("user_orders", "user_order", {
            "order_id": o["order_id"], "ticker": o["ticker"], "status": o["status"],
            "outcome_side": o.get("outcome_side"), "book_side": o.get("book_side"),
            "yes_price_dollars": o.get("yes_price_dollars"), "fill_count_fp": o.get("fill_count_fp"),
            "remaining_count_fp": o.get("remaining_count_fp"), "created_ts_ms": int(time.time() * 1000)})

    def drop_all(self):
        async def _x():
            for c in list(self.conns):
                await c.close()
        self.call(_x())
