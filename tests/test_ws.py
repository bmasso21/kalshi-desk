"""Phase 1.2 tests: WebSocket feed <-> mock Kalshi WS + REST, for Ed25519 and RSA keys.

Covers: signed handshake, bad-key rejection, snapshots, deltas, seq-gap resync, dynamic
add/delete markets, user_orders + REST seeding, fills stored once (WS + REST), reconnect after
a dropped connection, REST fallback while the feed is down, and the feed-driven quoter."""
import json
import sys
import tempfile
import threading
import time
from decimal import Decimal
from pathlib import Path

import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from mock_kalshi import create_mock  # noqa: E402
from mock_kalshi_ws import MockWS  # noqa: E402

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def wait(fn, timeout=10.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


def run(keytype: str, base_port: int):
    print(f"\n===== WebSocket feed, {keytype} key =====")
    from app.config import Config
    from app.db import DB
    from app.server import create_app

    tmp = tempfile.mkdtemp()
    key = ed25519.Ed25519PrivateKey.generate() if keytype == "ed25519" else \
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kpath = Path(tmp) / "k.key"
    kpath.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
    kid = "KEY-" + keytype
    rest = create_mock({kid: key.public_key()})
    srv = uvicorn.Server(uvicorn.Config(rest, host="127.0.0.1", port=base_port, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    ws = MockWS({kid: key.public_key()}, rest.state.S, base_port + 1)
    ws.start()
    rest.state.order_hooks.append(ws.order_event)

    # --- wrong key: handshake must be rejected, app stays on REST ----------------
    bad = ed25519.Ed25519PrivateKey.generate()
    bpath = Path(tmp) / "bad.key"
    bpath.write_bytes(bad.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
    cfgp = Path(tmp) / "config.json"
    cfgp.write_text(json.dumps({"env": "demo", "base_url_override": f"http://127.0.0.1:{base_port}/trade-api/v2",
                                "ws_url_override": f"ws://127.0.0.1:{base_port + 1}/trade-api/ws/v2",
                                "demo_key_id": kid, "demo_private_key_path": str(bpath)}))
    cfg = Config(cfgp)
    db = DB(Path(tmp) / "t.db")
    app = create_app(cfg, db, start_engine=False)
    eng = app.state.engine
    feed = eng.feed
    feed.start()
    check("bad signature -> handshake rejected (401)", wait(lambda: ws.auth_failures >= 1, 8))
    check("bad key -> feed not connected, mode=polling", not feed.live() and feed.status()["mode"] == "polling")

    # --- correct key via the normal config path (reconnects the feed) ------------
    cfg.update({"demo_private_key_path": str(kpath)})
    eng.connect()
    feed = eng.feed
    c = TestClient(app)
    tok = c.get("/api/session").json()["token"]
    H = {"X-Desk-Token": tok}
    check("good key -> connected", wait(feed.live, 10), feed.status())
    time.sleep(2.5)  # the old (bad-key) session thread winds down meanwhile
    check("old session thread can't mark the new session disconnected", feed.live(), feed.status())
    check("account channels subscribed", wait(lambda: {"user_orders", "fill", "market_positions"} <= set(feed.sids), 5),
          feed.sids)
    check("resting orders seeded over REST", wait(lambda: feed.orders_synced, 5))
    st = c.get("/api/status").json()
    check("/api/status reports live feed", st["feed"]["connected"] and st["feed"]["mode"] == "websocket", st["feed"])

    # --- market subscription + snapshot -----------------------------------------
    feed.want("test", ["KXA-1", "KXB-1"])
    check("orderbook + ticker subscribed for watched markets",
          wait(lambda: "orderbook_delta" in feed.sids and "ticker" in feed.sids, 5), feed.sids)
    check("snapshots synced both books", wait(lambda: feed.top("KXA-1") and feed.top("KXB-1"), 5), feed.status())
    t = feed.top("KXA-1")
    check("top of book from snapshot: YES bid .50 / ask .52", t and t["yes_bid"] == Decimal("0.5") and
          t["yes_ask"] == Decimal("0.52"), t)
    check("ticker channel stored", wait(lambda: "KXA-1" in feed.tickers, 3))

    # --- deltas -------------------------------------------------------------------
    ws.delta("KXA-1", "yes", "0.51", 30)
    check("delta adds a better YES bid (.51)", wait(lambda: feed.top("KXA-1")["yes_bid"] == Decimal("0.51"), 3),
          feed.top("KXA-1"))
    ws.delta("KXA-1", "yes", "0.51", -30)
    check("delta removes the level when qty hits 0", wait(lambda: feed.top("KXA-1")["yes_bid"] == Decimal("0.5"), 3),
          feed.top("KXA-1"))
    ws.delta("KXA-1", "no", "0.49", 25)
    check("NO-side delta -> YES ask .51", wait(lambda: feed.top("KXA-1")["yes_ask"] == Decimal("0.51"), 3),
          feed.top("KXA-1"))
    b = c.get("/api/book/KXA-1").json()
    check("/api/book serves the live book", b["source"] == "ws" and b["book"]["yes_asks"][0][0] == "0.5100", b)
    md = c.get("/api/market/KXA-1").json()
    check("/api/market overlays live prices", md["book_source"] == "ws" and md["market"]["yes_ask_dollars"] == "0.5100",
          (md["book_source"], md["market"]["yes_ask_dollars"]))

    # --- seq gap -> snapshot resync ---------------------------------------------
    gaps0 = feed.stats["gaps"]
    ws.delta("KXA-1", "no", "0.49", -25, skip_seq=True)   # client misses one message
    check("seq gap detected", wait(lambda: feed.stats["gaps"] > gaps0, 3), feed.stats)
    check("book resynced from fresh snapshot", wait(lambda: feed.top("KXA-1") and
                                                      feed.top("KXA-1")["yes_ask"] == Decimal("0.52"), 5),
          feed.top("KXA-1"))
    check("get_snapshot was requested", any(x.get("cmd") == "update_subscription" and
                                            (x.get("params") or {}).get("action") == "get_snapshot" for x in ws.commands))

    # --- dynamic add / delete ----------------------------------------------------
    feed.want_ui("KXC-1")
    check("UI market added via add_markets + snapshot", wait(lambda: feed.top("KXC-1") is not None, 5))
    feed.want("test", ["KXA-1"])
    check("dropped market removed (delete_markets)", wait(lambda: "KXB-1" not in feed.subscribed and
                                                          "KXB-1" not in feed.books, 5), feed.subscribed)
    check("delete_markets sent", any((x.get("params") or {}).get("action") == "delete_markets" for x in ws.commands))

    # --- user orders --------------------------------------------------------------
    r = c.post("/api/order", headers=H, json={"ticker": "KXA-1", "intent": "buy_yes", "price": "0.45", "count": 5})
    oid = r.json()["kalshi"]["order_id"]
    check("new resting order appears via user_orders", wait(lambda: any(o["order_id"] == oid
                                                                     for o in feed.resting_orders() or []), 3),
          (feed.status()["mode"], len(feed.orders)))
    c.delete(f"/api/order/{oid}?ticker=KXA-1", headers=H)
    check("cancelled order leaves the live order set", wait(lambda: all(o["order_id"] != oid
                                                                     for o in feed.resting_orders() or []), 3))

    # --- fills: stored once from WS, REST sync doesn't double count ------------------
    r = c.post("/api/order", headers=H, json={"ticker": "KXB-1", "intent": "buy_yes", "price": "0.57", "count": 3})
    foid = r.json()["kalshi"]["order_id"]
    fill = {"trade_id": "TR-1", "order_id": foid, "market_ticker": "KXB-1", "is_taker": True, "side": "yes",
            "action": "buy", "yes_price_dollars": "0.5700", "count_fp": "3.00", "fee_cost": "0.0300",
            "post_position_fp": "3.00", "purchased_side": "yes", "ts_ms": int(time.time() * 1000)}
    eng.positions_cache["ts"] = time.time()
    ws.push("fill", "fill", fill)
    check("fill stored from feed", wait(lambda: db.query("SELECT COUNT(*) n FROM fills")[0]["n"] == 1, 3))
    row = db.query("SELECT * FROM fills")[0] if db.query("SELECT * FROM fills") else {}
    check("fill linked to its order's source (manual)", row.get("order_source") == "manual" and row.get("source") == "ws", row)
    check("fill invalidates position cache", eng.positions_cache["ts"] == 0)
    rest.state.S["fills"] = [{**fill, "ticker": "KXB-1", "fill_id": "F-1", "created_time": "2026-11-10T21:00:00Z"}]
    n = eng.sync_fills()
    check("REST fill sync does not double count the same trade", n == 0 and
          db.query("SELECT COUNT(*) n FROM fills")[0]["n"] == 1, n)

    # --- dropped connection: REST fallback, then auto-reconnect + resubscribe ----------
    rc0 = feed.stats["reconnects"]
    ws.drop_all()
    check("drop -> feed reports polling", wait(lambda: not feed.live(), 3))
    b = c.get("/api/book/KXA-1").json()
    check("REST fallback while feed is down", b["source"] == "rest" and b["book"]["yes_asks"], b)
    check("auto-reconnects", wait(lambda: feed.live() and feed.stats["reconnects"] > rc0, 10), feed.status())
    check("books resynced after reconnect", wait(lambda: feed.top("KXA-1") is not None and feed.top("KXC-1") is not None, 5))
    check("orders re-seeded after reconnect", wait(lambda: feed.orders_synced, 5))

    # --- server gone for a while: stays on REST, comes back by itself ----------------
    ws.stop()
    check("server down -> polling", wait(lambda: not feed.live(), 3))
    md = c.get("/api/market/KXA-1").json()
    check("market view still works on REST", md["book_source"] == "rest" and md["market"]["ticker"] == "KXA-1")
    time.sleep(2)
    ws.start()
    check("reconnects when server returns", wait(feed.live, 35), feed.status())
    check("audit logs the outage", wait(lambda: any(a["kind"] == "feed_down" for a in db.query("SELECT kind FROM audit")), 3))

    # --- feed-driven quoter (demo mock) ---------------------------------------------
    rest.state.S["positions"].clear()
    cfg.update({"quoter": {"enabled": True, "select_by": "rewards", "min_daily_reward": 5, "max_markets": 5, "size": 10, "refresh_sec": 30}})
    q = eng.quoter
    q.start()
    check("quoter rests both sides", wait(lambda: set(q.quotes.get("KXA-1", {})) == {"yes", "no"}, 10), q.snapshot()["events"][:3])
    check("quoter's markets are on the feed", wait(lambda: "KXA-1" in feed.subscribed and feed.top("KXA-1"), 5))
    yes0 = q.quotes["KXA-1"]["yes"]["price"]
    # better YES bid appears 1c above ours: the quoter should re-join the top well before refresh_sec (30s)
    # (the mock REST market is moved too, since quoter re-reads market metadata over REST)
    m = rest.state.S["events"][0]["markets"][0]
    m.update(yes_bid_dollars="0.5100", no_ask_dollars="0.4900")
    ws.delta("KXA-1", "yes", "0.51", 100)
    t0 = time.time()
    ok = wait(lambda: q.quotes.get("KXA-1", {}).get("yes", {}).get("price") == "0.5100", 8)
    check("feed change wakes quoter -> requotes YES at .51 within seconds", ok and time.time() - t0 < 8,
          (yes0, q.quotes.get("KXA-1"), round(time.time() - t0, 1)))
    n_orders = len(rest.state.S["orders"])
    time.sleep(12)   # past the 10s grace: live order list must keep the quotes (no false 'gone' churn)
    check("no requote churn with live order state", len(rest.state.S["orders"]) - n_orders <= 0,
          len(rest.state.S["orders"]) - n_orders)
    cfg.update({"quoter": {"enabled": False}})
    check("disabling quoter cancels quotes", wait(lambda: not q.quotes, 35))
    q.stop()
    eng.stop()
    ws.stop()
    srv.should_exit = True


if __name__ == "__main__":
    run("ed25519", 8931)
    run("rsa", 8941)
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)
