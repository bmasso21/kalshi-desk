"""End-to-end test: desk server <-> strict mock Kalshi, for both Ed25519 and RSA keys."""
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from mock_kalshi import create_mock  # noqa: E402
from app.kalshi_client import d  # noqa: E402

PORT = 8911
OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def write_key(tmp, name, key):
    p = Path(tmp) / f"{name}.key"
    p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    return str(p)


def run(keytype: str):
    print(f"\n===== {keytype} key =====")
    from app.config import Config
    from app.db import DB
    from app.server import create_app

    tmp = tempfile.mkdtemp()
    key = ed25519.Ed25519PrivateKey.generate() if keytype == "ed25519" else \
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kpath = write_key(tmp, keytype, key)
    mock = create_mock({"KEY-" + keytype: key.public_key()})
    port = PORT + (0 if keytype == "ed25519" else 1)
    srv = uvicorn.Server(uvicorn.Config(mock, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)

    cfgp = Path(tmp) / "config.json"
    cfgp.write_text(json.dumps({"env": "demo", "base_url_override": f"http://127.0.0.1:{port}/trade-api/v2",
                                "demo_key_id": "KEY-" + keytype, "demo_private_key_path": kpath}))
    cfg = Config(cfgp)
    db = DB(Path(tmp) / "t.db")
    app = create_app(cfg, db, start_engine=False)
    eng = app.state.engine
    c = TestClient(app)
    tok = c.get("/api/session").json()["token"]
    H = {"X-Desk-Token": tok}

    # UI + status
    check("index served", c.get("/").status_code == 200 and "Kalshi Desk" in c.get("/").text)
    import re as _re
    page = c.get("/").text
    check("UI never relies on browser confirm()/prompt() boxes (blocked in embedded browsers): in-page dialog only",
          not _re.search(r"(?<![A-Za-z])(confirm|prompt|alert)\(", page) and 'id="dlgYes"' in page and "await ask('Switch to PRODUCTION?" in page)
    s = c.get("/api/status").json()
    check("status authenticated", s["authenticated"] is True, s)
    check("csrf blocks tokenless POST", c.post("/api/scan").status_code == 403)

    # scan
    eng.scan_once()
    sig = c.get("/api/signals").json()
    strat = {x["strategy"]: x for x in sig["signals"]}
    check("scan visited all paginated events", eng.scan_state["stats"].get("events") == 4, eng.scan_state)
    check("basket_no found on KXA", "basket_no" in strat and "KXA" in strat["basket_no"]["legs"][0]["ticker"], list(strat))
    bn = strat.get("basket_no")
    if bn:
        e = float(bn["edge"])
        check("basket_no edge ~0.10 after fees", 0.09 < e < 0.11, e)
    check("basket_yes found on KXD (flagged)", "basket_yes" in strat and "RULES" in strat["basket_yes"]["note"])
    check("wide_spread found on KXC", "wide_spread" in strat)
    check("movers found on KXC", "movers" in strat)
    check("no false basket on fair event B", not any("KXB" in l["ticker"] for x in sig["signals"]
                                                     if x["strategy"].startswith("basket") for l in x["legs"]))

    # market detail + fee quote
    md = c.get("/api/market/KXA-1").json()
    check("market detail + book", md["book"]["yes_asks"][0][0] == "0.5200" and md["fee_type"] == "quadratic", md["book"])
    f = c.get("/api/fee?price=0.5&count=100").json()
    check("taker fee 100 @ .50 = $1.75", f["taker"] == "1.75", f)
    check("maker fee quadratic = $0", f["maker"] == "0.00", f)

    # manual buy YES (crosses -> fills)
    r = c.post("/api/order", headers=H, json={"ticker": "KXB-1", "intent": "buy_yes", "price": "0.57", "count": 10})
    check("buy_yes placed + filled", r.status_code == 200 and r.json()["kalshi"]["fill_count"] == "10.00", r.text)
    lo = mock.state.last_order
    check("V2 body: bid 0.5700 count 10.00", lo["side"] == "bid" and lo["price"] == "0.5700" and lo["count"] == "10.00", lo)

    # buy NO -> ask at 1-q
    r = c.post("/api/order", headers=H, json={"ticker": "KXB-2", "intent": "buy_no", "price": "0.59", "count": 5})
    lo = mock.state.last_order
    check("buy_no -> ask @ 1-q (0.4100)", r.status_code == 200 and lo["side"] == "ask" and lo["price"] == "0.4100", (r.text, lo))

    # resting order then cancel
    r = c.post("/api/order", headers=H, json={"ticker": "KXA-1", "intent": "buy_yes", "price": "0.45", "count": 4})
    oid = r.json()["kalshi"]["order_id"]
    port_ = c.get("/api/portfolio").json()
    check("portfolio shows resting order", any(o["order_id"] == oid for o in port_["orders"]))
    check("portfolio positions", {p["ticker"] for p in port_["positions"]} >= {"KXB-1", "KXB-2"}, port_["positions"])
    check("cancel order", c.delete(f"/api/order/{oid}?ticker=KXA-1", headers=H).status_code == 200)

    # risk: per-order limit ($25 default)
    r = c.post("/api/order", headers=H, json={"ticker": "KXA-1", "intent": "buy_yes", "price": "0.52", "count": 100})
    check("risk blocks $52 order", r.status_code == 400 and "per-order" in r.json()["error"], r.text)
    # risk: longshot guard
    r = c.post("/api/order", headers=H, json={"ticker": "KXA-1", "intent": "buy_yes", "price": "0.01", "count": 5})
    check("risk blocks 1c longshot", r.status_code == 400, r.text)
    # can't sell what you don't hold
    r = c.post("/api/order", headers=H, json={"ticker": "KXA-3", "intent": "sell_yes", "price": "0.25", "count": 5})
    check("blocks naked sell_yes", r.status_code == 400 and "hold" in r.json()["error"], r.text)
    # close held YES
    r = c.post("/api/order", headers=H, json={"ticker": "KXB-1", "intent": "sell_yes", "price": "0.55", "count": 10,
                                               "time_in_force": "immediate_or_cancel"})
    check("close YES position", r.status_code == 200 and mock.state.last_order["side"] == "ask", r.text)

    # approve basket_no signal with 5 baskets
    if bn:
        r = c.post(f"/api/signals/{bn['id']}/approve", headers=H, json={"count": 5})
        j = r.json()
        check("basket_no executed all 3 legs FOK", r.status_code == 200 and j["status"] == "executed"
              and mock.state.last_order["time_in_force"] == "fill_or_kill", r.text)

    # daily limit
    cfg.update({"risk": {"max_daily_spend": 20}})
    r = c.post("/api/order", headers=H, json={"ticker": "KXA-2", "intent": "buy_yes", "price": "0.42", "count": 20})
    check("daily spend limit blocks", r.status_code == 400 and "Daily" in r.json()["error"], r.text)
    cfg.update({"risk": {"max_daily_spend": 100}})

    # kill switch cancels + blocks
    c.post("/api/order", headers=H, json={"ticker": "KXA-1", "intent": "buy_yes", "price": "0.40", "count": 2})
    k = c.post("/api/kill", headers=H, json={"on": True}).json()
    check("kill switch cancels resting", len(k["cancelled"]) >= 1, k)
    r = c.post("/api/order", headers=H, json={"ticker": "KXA-1", "intent": "buy_yes", "price": "0.40", "count": 2})
    check("kill switch blocks orders", r.status_code == 400 and "Kill" in r.json()["error"], r.text)
    c.post("/api/kill", headers=H, json={"on": False})

    # fair value strategy
    c.post("/api/fair_values", headers=H, json={"ticker": "KXB-2", "fair_yes": "0.55", "note": "test"})
    eng.scan_once()
    fv = [x for x in c.get("/api/signals").json()["signals"] if x["strategy"] == "fair_value"]
    check("fair_value flags KXB-2 buy_yes (ask .43 vs fair .55)", any(x["legs"][0]["intent"] == "buy_yes" for x in fv), fv)

    # auto mode: basket_yes must never auto-trade, basket_no may
    before = len([o for o in db.query("SELECT * FROM orders_log WHERE source='auto'")])
    cfg.update({"auto": {"enabled": True, "strategies": ["basket_yes"], "max_order_cost": 10, "max_daily_spend": 30}})
    eng.scan_once()
    after = len(db.query("SELECT * FROM orders_log WHERE source='auto'"))
    check("auto never trades basket_yes", after == before)
    cfg.update({"auto": {"strategies": ["basket_no"]}})
    eng.scan_once()
    autos = db.query("SELECT * FROM orders_log WHERE source='auto' AND status='placed'")
    check("auto trades basket_no within auto limits", len(autos) == 3 and all(float(a["cost"]) <= 10 for a in autos), autos)
    cfg.update({"auto": {"enabled": False}})

    # --- v1.1: blocked-order logging, fill-based spend, 10c floor, loss limit, quoter -----
    r = c.post("/api/order", headers=H, json={"ticker": "KXA-1", "intent": "buy_yes", "price": "0.05", "count": 5})
    check("10c floor blocks 5c buy", r.status_code == 400 and "0.1" in r.json()["error"], r.text)
    lg = c.get("/api/ledger").json()["orders"]
    check("blocked orders appear in ledger", any(o["status"] == "blocked" for o in lg), [o["status"] for o in lg][:8])
    before = eng.spent_today(max_age=0)["all"]
    r = c.post("/api/order", headers=H, json={"ticker": "KXA-2", "intent": "buy_yes", "price": "0.30", "count": 5,
                                               "post_only": True})
    oid = r.json()["kalshi"]["order_id"]
    mid_spend = eng.spent_today(max_age=0)
    c.delete(f"/api/order/{oid}?ticker=KXA-2", headers=H)
    after = eng.spent_today(max_age=0)["all"]
    check("unfilled resting order adds $0 spend", mid_spend["all"] == before and mid_spend["resting_buy"] > 0,
          (before, mid_spend))
    check("cancelled order adds $0 spend", after == before, (before, after))

    # quoter
    cfg.update({"quoter": {"enabled": True, "select_by": "rewards", "min_daily_reward": 5, "max_markets": 5, "size": 10}})
    q = eng.quoter
    q.cycle()
    snap = c.get("/api/quoter").json()
    sel = [s["ticker"] for s in snap["selected"]]
    check("quoter picks $10/day pool, skips $0.30/day", sel == ["KXA-1"], snap["selected"])
    qs = {x["side"]: x for x in snap["quotes"]}
    check("quoter rests both sides at top of book", set(qs) == {"yes", "no"} and qs["yes"]["price"] == "0.5000"
          and qs["no"]["price"] == "0.4800", snap["quotes"])
    lo = [o for o in mock.state.S["orders"].values() if o["ticker"] == "KXA-1" and o["status"] == "resting"]
    check("quotes are post-only with expiry", len(lo) == 2 and all(o["post_only"] and o["expiration_time"] for o in lo), lo)
    check("quotes live ~10 min (quote_ttl_sec) so they keep queue priority",
          all(570 <= int(o["expiration_time"]) - time.time() <= 610 for o in lo), [o["expiration_time"] for o in lo])
    n_orders = len(mock.state.S["orders"])
    q.cycle()
    check("unchanged book -> no requote churn", len(mock.state.S["orders"]) == n_orders)
    mk = mock.state.S["events"][0]["markets"][0]
    mk.update(yes_bid_dollars="0.5500", yes_ask_dollars="0.5700", no_bid_dollars="0.4300", no_ask_dollars="0.4500")
    q.cycle()
    snap = c.get("/api/quoter").json()
    check("5c jump pulls quotes + cooldown", not snap["quotes"] and any(e["kind"] == "jump" for e in snap["events"]),
          snap["quotes"])
    q.cooldown.clear()
    mock.state.S["positions"]["KXA-1"] = {"pos": d("35"), "exp": d("17"), "fees": d("0")}
    eng.positions_cache["ts"] = 0
    q.cycle()
    sides = {x["side"] for x in c.get("/api/quoter").json()["quotes"]}
    check("inventory cap: long 35 YES -> only NO quoted", sides == {"no"}, sides)
    lo = [o for o in mock.state.S["orders"].values() if o["ticker"] == "KXA-1" and o["status"] == "resting"]
    check("exit: at the cap the reducing quote is 1c inside the spread (ask YES .56 between .55/.57)",
          len(lo) == 1 and lo[0]["book_side"] == "ask" and lo[0]["yes_price_dollars"] == "0.5600", lo)
    lg = db.query("SELECT intent, count FROM orders_log WHERE source='quoter' ORDER BY id DESC LIMIT 1")[0]
    check("exit: sent as a SELL of the held YES (closing order, exempt from buy limits)", lg["intent"] == "sell_yes" and float(lg["count"]) == 10, lg)
    # capital cap can't block an exit; small position -> exit sized to the position, other side still opens
    old_cap = cfg.quoter["max_capital"]
    cfg.update({"quoter": {"max_capital": 1}})
    mock.state.S["positions"]["KXA-1"] = {"pos": d("4"), "exp": d("2"), "fees": d("0")}
    eng.positions_cache["ts"] = 0
    q.cycle()
    qs = {x["side"]: x for x in c.get("/api/quoter").json()["quotes"]}
    check("exit: allowed even when the capital cap is used up; opening side blocked by the cap",
          set(qs) == {"no"} and qs["no"]["reduce"] and float(qs["no"]["count"]) == 4 and qs["no"]["price"] == "0.5700", qs)   # sell YES at the .57 ask
    cfg.update({"quoter": {"max_capital": old_cap}})
    q.cycle()
    qs = {x["side"]: x for x in c.get("/api/quoter").json()["quotes"]}
    check("small long: YES side keeps bidding, NO side exits", set(qs) == {"yes", "no"} and not qs["yes"]["reduce"] and qs["no"]["reduce"], qs)
    mock.state.S["positions"]["KXA-1"] = {"pos": d("-35"), "exp": d("17"), "fees": d("0")}
    eng.positions_cache["ts"] = 0
    q.cycle()
    lg = db.query("SELECT intent, price, count FROM orders_log WHERE source='quoter' AND status='placed' ORDER BY id DESC LIMIT 1")[0]
    lo = [o for o in mock.state.S["orders"].values() if o["ticker"] == "KXA-1" and o["status"] == "resting"]
    check("short 35 (NO) at the cap: only a reducing bid, 1c inside (bid YES .56 = sell NO .44)",
          len(lo) == 1 and lo[0]["book_side"] == "bid" and lo[0]["yes_price_dollars"] == "0.5600" and lg["intent"] == "sell_no", (lo, lg))
    mock.state.S["positions"]["KXA-1"] = {"pos": d("35"), "exp": d("17"), "fees": d("0")}
    eng.positions_cache["ts"] = 0
    q.cycle()
    # stray orders: one the quoter sent but lost track of is cancelled; a manual order is never touched
    tracked = {x["oid"] for sd in q.quotes.values() for x in sd.values()}
    lost = eng.place(ticker="KXA-1", intent="buy_yes", price="0.50", count=1, source="quoter", strategy="liquidity", post_only=True)
    r_man = c.post("/api/order", headers=H, json={"ticker": "KXB-1", "intent": "buy_yes", "price": "0.30", "count": 1}).json()
    man_oid = r_man["kalshi"]["order_id"]
    n = q.sweep_strays(eng.client)
    O = mock.state.S["orders"]
    check("stray sweep: untracked quoter order cancelled", n == 1 and O[lost["kalshi"]["order_id"]]["status"] == "canceled", n)
    check("stray sweep: tracked quotes and manual orders untouched", O[man_oid]["status"] == "resting"
          and all(O[o]["status"] == "resting" for o in tracked))
    c.delete(f"/api/order/{man_oid}?ticker=KXB-1", headers=H)
    # lost reply: logged as unknown with its client id
    real_create = eng.client.create_order

    def boom(*a, **k):
        import requests as _rq
        raise _rq.ConnectionError("Connection aborted.")
    eng.client.create_order = boom
    try:
        eng.place(ticker="KXB-1", intent="buy_yes", price="0.30", count=1, source="manual")
        unk = False
    except Exception:
        unk = True
    eng.client.create_order = real_create
    row = db.query("SELECT status, client_order_id, error FROM orders_log ORDER BY id DESC LIMIT 1")[0]
    check("lost reply: order logged as 'unknown' with its client id + audit", unk and row["status"] == "unknown" and row["client_order_id"]
          and db.query("SELECT 1 FROM audit WHERE kind='order_unknown'"), row)
    # resting buy orders count toward exposure limits
    lim = cfg.risk["max_market_exposure"]
    r1 = c.post("/api/order", headers=H, json={"ticker": "KXB-1", "intent": "buy_yes", "price": "0.30", "count": 60})   # $18 resting
    cfg.update({"risk": {"max_market_exposure": 30}})
    r2 = c.post("/api/order", headers=H, json={"ticker": "KXB-1", "intent": "buy_yes", "price": "0.30", "count": 60})   # 18 + 18 > 30
    check("resting orders count toward the per-market limit", r1.status_code == 200 and r2.status_code == 400 and "resting" in r2.text, r2.text)
    r3 = c.post("/api/order", headers=H, json={"ticker": "KXB-1", "intent": "buy_yes", "price": "0.30", "count": 30})   # 18 + 9 <= 30
    check("...and an order that still fits is accepted", r3.status_code == 200, r3.text)
    for o in list(O.values()):
        if o["ticker"] == "KXB-1" and o["status"] == "resting":
            c.delete(f"/api/order/{o['order_id']}?ticker=KXB-1", headers=H)
    cfg.update({"risk": {"max_market_exposure": lim}})
    cfg.update({"quoter": {"enabled": False}})
    q.cycle()
    still = [o for o in mock.state.S["orders"].values() if o["ticker"] == "KXA-1" and o["status"] == "resting"]
    check("disabling quoter cancels its quotes", not still and not q.quotes, still)
    rw = c.get("/api/rewards").json()
    check("rewards tab shows credits", rw["credits_total"] == "3.2100", rw.get("credits_total"))

    # daily loss limit
    cfg.update({"quoter": {"enabled": True}})
    eng.daily_pnl()  # snapshot start equity
    mock.state.S["balance"] -= 3000  # -$30
    eng.check_loss_limit()
    check("daily loss limit trips kill switch", cfg.risk["kill_switch"] and not cfg.quoter["enabled"]
          and not cfg.auto["enabled"])
    check("loss-limit kill is audited", any(a["kind"] == "KILL" for a in c.get("/api/ledger").json()["audit"]))
    c.post("/api/kill", headers=H, json={"on": False})
    db.execute("DELETE FROM day_equity")

    # prod confirm flow
    cfg.update({"env": "prod", "prod_key_id": "KEY-" + keytype, "prod_private_key_path": kpath})
    eng.connect()
    r = c.post("/api/order", headers=H, json={"ticker": "KXB-1", "intent": "buy_yes", "price": "0.57", "count": 1})
    check("prod requires confirm token", r.status_code == 409 and r.json()["needs_confirm"], r.text)
    t = r.json()["confirm_token"]
    r = c.post("/api/order", headers=H, json={"ticker": "KXB-1", "intent": "buy_yes", "price": "0.57", "count": 1, "confirm_token": t})
    check("prod order after confirm", r.status_code == 200, r.text)
    cfg.update({"env": "demo"})

    # settlements + ledger
    st_ = c.get("/api/settlements").json()
    check("settlements load", st_["settlements"][0]["revenue"] == 1000)
    lg = c.get("/api/ledger").json()
    check("ledger logs placed + rejected", {"placed"} <= {o["status"] for o in lg["orders"]})
    pub = c.get("/api/config").json()["config"]
    check("config view hides key path", pub["demo_private_key_path"] is True and "…" in pub["demo_key_id"])
    srv.should_exit = True


if __name__ == "__main__":
    run("ed25519")
    run("rsa")
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)
