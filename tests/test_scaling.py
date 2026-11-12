"""v3.2 tests: limits that scale with the account (tiers, caps, daily base), transfer-aware loss tracking, exit rules."""
import json
import socket
import sys
import tempfile
import threading
import time
from decimal import Decimal
from pathlib import Path

import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from mock_kalshi import create_mock  # noqa: E402
from app import scaling as SC  # noqa: E402

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def close(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def unit_tests():
    a = SC.compute(10)
    check("$10 account: the approved percentages", a["tier"] == 0 and a["limits"] == {
        "order": 2.0, "daily_buys": 5.0, "market": 3.0, "total": 8.0, "daily_loss": 2.0, "strat_day": 1.5, "strat_7d": 3.0,
        "drawdown": 3.0, "quoter": 8.0}, a)
    b = SC.compute(110)["limits"]
    check("$110 account", b["order"] == 22 and b["daily_buys"] == 55 and b["market"] == 33 and b["total"] == 88 and b["daily_loss"] == 22
          and b["strat_day"] == 16.5 and b["drawdown"] == 33, b)
    c = SC.compute(1000)
    check("$1,000: tightest tier percentages no longer bind below the previous tier's top value",
          c["tier"] == 2 and c["limits"]["order"] == 100 and c["limits"]["daily_loss"] == 120, c)
    check("$5,000: 5% per order, 10% daily loss", SC.compute(5000)["limits"]["order"] == 250 and SC.compute(5000)["limits"]["daily_loss"] == 500)
    prev, mono = None, True
    for e in range(1, 6001, 7):
        lim = SC.compute(e)["limits"]
        if prev and any(lim[k] < prev[k] - 1e-9 for k in SC.KEYS):
            mono = False
        prev = lim
    check("no limit ever drops as the account grows (across both tier boundaries)", mono)
    ok = all(SC.compute(e)["limits"][k] <= e + 1e-9 for e in (1, 10, 250, 999, 4000) for k in SC.KEYS if k != "daily_buys")
    check("no single limit exceeds the account value", ok)
    check("caps put a ceiling on a limit", SC.compute(5000, caps={"order": 50})["limits"]["order"] == 50
          and SC.compute(10, caps={"order": 50})["limits"]["order"] == 2)
    check("zero / negative equity -> zero limits", set(SC.compute(-5)["limits"].values()) == {0.0})
    check("quoter shape: $8 -> 1 market, 4 contracts, 8 inventory", SC.quoter_shape(8, 5) == {"max_markets": 1, "size": 4, "max_inventory": 8})
    check("quoter shape: $88 -> 5 markets, 8 contracts, 16 inventory", SC.quoter_shape(88, 5) == {"max_markets": 5, "size": 8, "max_inventory": 16})
    p = SC.to_patch(SC.compute(100)["limits"], 5)
    check("auto limits keep their ratio (40% of order, 30% of daily buys)", p["auto"] == {"max_order_cost": 8.0, "max_daily_spend": 15.0}, p["auto"])


def api_tests(tmp):
    from app.config import Config
    from app.db import DB
    from app.server import create_app
    key = ed25519.Ed25519PrivateKey.generate()
    kp = Path(tmp) / "k.key"
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    mock = create_mock({"KEY-1": key.public_key()})
    S = mock.state.S
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(mock, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    cfgp = Path(tmp) / "config.json"
    cfgp.write_text(json.dumps({"env": "demo", "base_url_override": f"http://127.0.0.1:{port}/trade-api/v2",
                                "demo_key_id": "KEY-1", "demo_private_key_path": str(kp)}))
    cfg = Config(cfgp)
    db = DB(Path(tmp) / "t.db")
    app = create_app(cfg, db, start_engine=False)
    eng = app.state.engine
    c = TestClient(app)
    H = {"X-Desk-Token": c.get("/api/session").json()["token"]}

    # ---- scaling ----
    check("off by default; fixed limits untouched", cfg.data["scaling"]["enabled"] is False and cfg.risk["max_order_cost"] == 25)
    check("recalc refused while off", c.post("/api/scaling/recalc", headers=H).status_code == 400)
    S["balance"], S["pv"] = 1000, 0                     # $10.00 account
    r = c.post("/api/config", headers=H, json={"scaling": {"enabled": True, "base": 99999, "limits": {"order": 999}}})
    check("turning it on applies the limits from the account value at once", r.status_code == 200 and cfg.data["scaling"]["base"] == 10.0
          and cfg.risk["max_order_cost"] == 2 and cfg.risk["max_daily_spend"] == 5 and cfg.risk["max_market_exposure"] == 3
          and cfg.risk["max_total_exposure"] == 8 and cfg.risk["max_daily_loss"] == 2, (r.text[:200], cfg.risk))
    check("breakers, auto and quoter follow too", cfg.data["breakers"]["strategy_daily_loss"] == 1.5 and cfg.data["breakers"]["account_drawdown"] == 3
          and cfg.auto["max_order_cost"] == 0.8 and cfg.quoter["max_capital"] == 8 and cfg.quoter["size"] == 4
          and cfg.quoter["max_markets"] == 1 and cfg.quoter["max_inventory"] == 8, (cfg.data["breakers"], cfg.auto, cfg.quoter))
    check("turning scaling on does not switch the quoter or auto-trading on", not cfg.quoter["enabled"] and not cfg.auto["enabled"] and cfg.env == "demo")
    r = c.post("/api/config", headers=H, json={"risk": {"max_order_cost": 500, "min_price": 0.2}, "quoter": {"max_capital": 9999, "pull_hours": 7}})
    check("while on, hand-typed dollar limits are ignored; other settings still save", cfg.risk["max_order_cost"] == 2 and cfg.risk["min_price"] == 0.2
          and cfg.quoter["max_capital"] == 8 and cfg.quoter["pull_hours"] == 7, (cfg.risk, cfg.quoter))
    r = c.post("/api/order", headers=H, json={"ticker": "KXA-1", "intent": "buy_yes", "price": "0.40", "count": 6})   # $2.40 > $2
    check("orders are held to the scaled limit", r.status_code == 400 and "per-order limit $2.00" in r.text, r.text)
    S["balance"] = 11000                               # deposit -> $110
    eng.scaler.apply()
    check("same day: the base stays fixed after a deposit", cfg.data["scaling"]["base"] == 10.0 and cfg.risk["max_order_cost"] == 2)
    cfg.data["scaling"]["base_day"] = "2000-01-01"
    eng.check_loss_limit()
    check("next morning: limits follow the new account value ($110)", cfg.data["scaling"]["base"] == 110.0 and cfg.risk["max_order_cost"] == 22
          and cfg.risk["max_total_exposure"] == 88 and cfg.quoter["size"] == 8 and cfg.quoter["max_markets"] == 5, (cfg.data["scaling"], cfg.quoter))
    S["balance"] = 100000
    r = c.post("/api/scaling/recalc", headers=H)
    check("Recalculate now: $1,000 -> tier 3", r.status_code == 200 and r.json()["tier"] == 2 and cfg.risk["max_order_cost"] == 100, r.text[:300])
    r = c.post("/api/config", headers=H, json={"scaling": {"caps": {"order": 30, "bogus": 5}}})
    check("a cap is applied immediately; unknown cap names dropped", cfg.risk["max_order_cost"] == 30 and "bogus" not in cfg.data["scaling"]["caps"], cfg.data["scaling"]["caps"])
    r = c.post("/api/config", headers=H, json={"scaling": {"tiers": [{"upto": 100, "order": 10}]}})
    check("malformed tiers rejected", r.status_code == 400 and "tiers" not in {k for k, v in cfg.data["scaling"].items() if k == "tiers" and v})
    sn = c.get("/api/scaling").json()
    check("status: tiers, examples and current limits", sn["enabled"] and sn["examples"]["10"]["order"] == 2.0 and len(sn["tiers"]) == 3)
    c.post("/api/config", headers=H, json={"scaling": {"enabled": False}, "risk": {"max_order_cost": 25, "max_daily_spend": 100,
                                                                                    "max_market_exposure": 50, "max_total_exposure": 250, "max_daily_loss": 25}})
    check("turning it off: fixed limits are editable again", cfg.risk["max_order_cost"] == 25 and not cfg.data["scaling"]["enabled"])
    check("settings page has the scaling card and exit columns", "Scale limits with the account" in c.get("/").text and "Auto-exit" in c.get("/").text)

    # ---- transfers are not profit or loss ----
    S["balance"], S["pv"] = 10000, 0                   # $100
    db.execute("DELETE FROM day_equity")
    p0 = eng.daily_pnl()
    now = int(time.time())
    S["balance"] = 4000                                # $60 withdrawn
    S["withdrawals"] = [{"id": "w1", "status": "applied", "type": "ach", "amount_cents": 5900, "fee_cents": 100, "created_ts": now, "finalized_ts": now + 1},
                        {"id": "w2", "status": "pending", "type": "ach", "amount_cents": 99900, "fee_cents": 0, "created_ts": now}]
    eng.transfers._cache["ts"] = 0
    p1 = eng.daily_pnl()
    check("withdrawal (incl. its fee) is not a loss today; pending ones ignored", close(float(p0["pnl"]), 0) and close(float(p1["pnl"]), 0) and close(float(p1["transfers"]), -60), p1)
    cfg.update({"risk": {"max_daily_loss": 25, "kill_switch": False}})
    eng.check_loss_limit()
    check("...so it does not trip the daily loss limit", not cfg.risk["kill_switch"])
    a0 = eng.breakers.account(time.time())
    cfg.update({"breakers": {"account_drawdown": 20}})
    S["deposits"] = [{"id": "d1", "status": "applied", "type": "debit", "amount_cents": 50000, "fee_cents": 0, "created_ts": (now + 2) * 1000}]
    S["balance"] = 54000                               # +$500 deposit (timestamp in ms)
    eng.transfers._cache["ts"] = 0
    a1 = eng.breakers.account(time.time())
    S["balance"] = 52500                               # real $15 loss
    a2 = eng.breakers.account(time.time())
    check("deposit is not profit: the drawdown peak does not jump", close(a0["drawdown"], 0) and close(a1["drawdown"], 0) and close(a2["drawdown"], 15) and not cfg.risk["kill_switch"], (a0, a1, a2))
    S["balance"] = 51900                               # real loss now $21 >= $20
    a3 = eng.breakers.account(time.time())
    check("a real loss past the limit still trips the kill switch", cfg.risk["kill_switch"] and a3.get("tripped"), a3)
    cfg.update({"risk": {"kill_switch": False}, "breakers": {"account_drawdown": 0}})
    real_pag = eng.client.paginate

    def broken(*a, **k):
        raise RuntimeError("404 not offered here")
    eng.client.paginate = broken
    eng.transfers._cache = {"ts": 0.0, "env": None, "rows": []}
    try:
        v = eng.transfers.net(0)
    except Exception:
        v = "raised"
    eng.client.paginate = real_pag
    check("transfer history unavailable -> no adjustment, no crash", v == 0 and eng.transfers.error, v)
    eng.transfers._cache["ts"] = 0

    # ---- exit rules ----
    ex = eng.exits
    check("default: a condition must hold on 2 checks in a row", cfg.data["exits"]["confirm_checks"] == 2)
    cfg.update({"exits": {"confirm_checks": 1}})
    S["balance"] = 100000
    r = c.post("/api/exits", headers=H, json={"ticker": "KXA-1", "take_profit": 0.6})
    check("no position -> no rule", r.status_code == 400 and "no position" in r.text, r.text)
    S["positions"]["KXA-1"] = {"pos": Decimal("20"), "exp": Decimal("8"), "fees": Decimal("0")}      # long 20 YES; bid .50
    eng.positions_cache["ts"] = 0
    bad = [c.post("/api/exits", headers=H, json=b).status_code for b in (
        {"ticker": "KXA-1"}, {"ticker": "KXA-1", "take_profit": 0.4, "stop_loss": 0.45}, {"ticker": "KXA-1", "take_profit": 1.5},
        {"ticker": "KXA-1", "floor": 0.3}, {"ticker": "KXA-1", "stop_loss": 0.3, "floor": 0.35})]
    check("invalid rules rejected (empty, take <= stop, out of range, floor without / above stop)", bad == [400] * 5, bad)
    r = c.post("/api/exits", headers=H, json={"ticker": "KXA-1", "take_profit": 0.60, "stop_loss": 0.35})
    check("rule saved for the side held", r.status_code == 200 and r.json()["side"] == "yes", r.text)
    n0 = len(S["orders"])
    check("bid .50 between stop .35 and take .60 -> nothing happens", ex.check() == [] and len(S["orders"]) == n0)
    mk = S["events"][0]["markets"][0]
    mk.update(yes_bid_dollars="0.6200", yes_ask_dollars="0.6400", no_bid_dollars="0.3600", no_ask_dollars="0.3800")
    out = ex.check()
    last = app.state.engine.db.query("SELECT intent, price, count, source FROM orders_log ORDER BY id DESC LIMIT 1")[0]
    check("take-profit: sells the whole position at the bid, immediate-or-cancel", len(out) == 1 and out[0]["sold"] == 20
          and last["intent"] == "sell_yes" and float(last["price"]) == 0.62 and last["source"] == "exit"
          and mock.state.last_order["time_in_force"] == "immediate_or_cancel", (out, last))
    check("position is flat and the rule is finished", S["positions"]["KXA-1"]["pos"] == 0 and ex.rules() == []
          and ex.rules(active_only=False)[0]["status"] == "triggered")
    check("exit is audited (-> phone alert)", db.query("SELECT 1 FROM audit WHERE kind='EXIT'"))
    # NO position, stop-loss with a floor
    S["positions"]["KXB-1"] = {"pos": Decimal("-10"), "exp": Decimal("4"), "fees": Decimal("0")}     # 10 NO; KXB-1 .55/.57 -> NO bid .43
    eng.positions_cache["ts"] = 0
    r = c.post("/api/exits", headers=H, json={"ticker": "KXB-1", "stop_loss": 0.40, "floor": 0.30})
    check("NO position: rule in NO prices", r.status_code == 200 and r.json()["side"] == "no")
    check("NO bid .43 above the stop -> hold", ex.check() == [])
    mb = S["events"][1]["markets"][0]
    mb.update(yes_bid_dollars="0.7300", yes_ask_dollars="0.7500", no_bid_dollars="0.2500", no_ask_dollars="0.2700")   # NO bid .25 < floor
    n1 = len(S["orders"])
    out = ex.check()
    check("stop hit but bid below the floor: NOT sold, alert once", out == [] and len(S["orders"]) == n1
          and len(db.query("SELECT 1 FROM audit WHERE kind='EXIT' AND message LIKE '%NOT sold%'")) == 1)
    ex.check()
    check("...no repeat alert while it waits", len(db.query("SELECT 1 FROM audit WHERE kind='EXIT' AND message LIKE '%NOT sold%'")) == 1)
    mb.update(yes_bid_dollars="0.6300", yes_ask_dollars="0.6500", no_bid_dollars="0.3500", no_ask_dollars="0.3700")   # NO bid .35
    cfg.update({"risk": {"kill_switch": True}})
    out = ex.check()
    cfg.update({"risk": {"kill_switch": False}})
    last = db.query("SELECT intent, price, count FROM orders_log WHERE status='placed' ORDER BY id DESC LIMIT 1")[0]
    check("stop-loss sells the NO position at the NO bid - even with the kill switch on", len(out) == 1 and out[0]["sold"] == 10
          and last["intent"] == "sell_no" and float(last["price"]) == 0.35 and S["positions"]["KXB-1"]["pos"] == 0, (out, last))
    # no bid -> no order; position closed by hand -> rule ends; never opens a position
    S["positions"]["KXC-1"] = {"pos": Decimal("5"), "exp": Decimal("2"), "fees": Decimal("0")}
    eng.positions_cache["ts"] = 0
    c.post("/api/exits", headers=H, json={"ticker": "KXC-1", "stop_loss": 0.90})
    mc = S["events"][2]["markets"][0]
    mc.update(yes_bid_dollars="0.0000")
    n2 = len(S["orders"])
    check("no bid at all -> nothing is sent", ex.check() == [] and len(S["orders"]) == n2)
    S["positions"]["KXC-1"]["pos"] = Decimal("0")
    mc.update(yes_bid_dollars="0.3000")
    out = ex.check()
    check("position already closed -> rule ends without trading", out == [] and len(S["orders"]) == n2
          and ex.rules(active_only=False)[0]["status"] == "closed")
    S["positions"]["KXC-1"]["pos"] = Decimal("300")
    eng.positions_cache["ts"] = 0
    cfg.update({"risk": {"max_contracts_per_order": 100}})
    c.post("/api/exits", headers=H, json={"ticker": "KXC-1", "stop_loss": 0.90})
    out = ex.check()
    check("large position sold in chunks of the max contracts per order", out and out[0]["sold"] == 300 and S["positions"]["KXC-1"]["pos"] == 0, out)
    # confirmation + resting orders cleared first (KXD-1: .28/.30)
    cfg.update({"exits": {"confirm_checks": 2}, "risk": {"max_contracts_per_order": 500}})
    S["positions"]["KXD-1"] = {"pos": Decimal("10"), "exp": Decimal("3"), "fees": Decimal("0")}
    eng.positions_cache["ts"] = 0
    resting_sell = eng.place(ticker="KXD-1", intent="sell_yes", price="0.35", count=10, source="manual")   # rests above the bid
    other = c.post("/api/order", headers=H, json={"ticker": "KXB-1", "intent": "buy_yes", "price": "0.30", "count": 1}).json()["kalshi"]["order_id"]
    c.post("/api/exits", headers=H, json={"ticker": "KXD-1", "stop_loss": 0.30})
    md = S["events"][3]["markets"][0]
    out1 = ex.check()
    check("first check with the stop hit: nothing sold yet", out1 == [] and S["positions"]["KXD-1"]["pos"] == 10)
    md.update(yes_bid_dollars="0.4000", yes_ask_dollars="0.4200")
    ex.check()
    md.update(yes_bid_dollars="0.2800", yes_ask_dollars="0.3000")
    out2 = ex.check()
    check("a one-off dip resets the count (dip, recover, dip = still no sale)", out2 == [] and S["positions"]["KXD-1"]["pos"] == 10)
    out3 = ex.check()
    check("second consecutive check: sold", len(out3) == 1 and out3[0]["sold"] == 10 and S["positions"]["KXD-1"]["pos"] == 0, out3)
    check("resting orders in that market were cancelled first (no overselling); other markets untouched",
          S["orders"][resting_sell["kalshi"]["order_id"]]["status"] == "canceled" and S["orders"][other]["status"] == "resting")
    c.delete(f"/api/order/{other}?ticker=KXB-1", headers=H)
    cfg.update({"exits": {"confirm_checks": 1}})
    cfg.update({"exits": {"enabled": False}})
    S["positions"]["KXC-1"]["pos"] = Decimal("5")
    c.post("/api/exits", headers=H, json={"ticker": "KXC-1", "stop_loss": 0.90})
    check("master switch off -> rules don't fire", ex.check() == [] and S["positions"]["KXC-1"]["pos"] == 5)
    cfg.update({"exits": {"enabled": True}})
    check("rule can be removed", c.delete("/api/exits/KXC-1", headers=H).json()["removed"] and ex.rules() == [])
    # ---- quoter: exit-only markets and the per-market loss stop ----
    q = eng.quoter
    for o in list(S["orders"].values()):
        o["status"] = "canceled" if o["status"] == "resting" else o["status"]
    for t_ in list(S["positions"]):
        S["positions"][t_]["pos"] = Decimal("0")
    S["events"][1]["markets"][0].update(yes_bid_dollars="0.5500", yes_ask_dollars="0.5700", no_bid_dollars="0.4300", no_ask_dollars="0.4500")
    S["events"][0]["markets"][0].update(yes_bid_dollars="0.5000", yes_ask_dollars="0.5200", no_bid_dollars="0.4800", no_ask_dollars="0.5000")
    S["balance"] = 100000
    cfg.update({"risk": {"kill_switch": False, "max_daily_loss": 0}, "quoter": {"enabled": True, "select_by": "rewards", "max_capital": 100,
                                                                                 "size": 10, "max_inventory": 30, "market_loss_pct": 10}})
    q.programs_ts, q.selected = time.time(), []            # nothing selected ...
    S["positions"]["KXB-1"] = {"pos": Decimal("6"), "exp": Decimal("3"), "fees": Decimal("0")}      # ... but the quoter still holds 6 YES here
    db.execute("INSERT INTO fills(env,fill_key,ticker,ts,yes_price,count,fee,book_side,strategy,order_source) VALUES(?,?,?,?,?,?,?,?,?,?)",
               ("demo", "q1", "KXB-1", time.time() - 3 * 86400, "0.5500", "6", "0", "bid", "liquidity", "quoter"))
    eng.positions_cache["ts"] = 0
    q.__dict__.pop("_own_cache", None)
    q.cycle()
    lo = [o for o in S["orders"].values() if o["status"] == "resting"]
    check("quoter keeps working out of a market it no longer selects: one sell of the 6 held, nothing opened",
          len(lo) == 1 and lo[0]["ticker"] == "KXB-1" and lo[0]["book_side"] == "ask" and float(lo[0]["remaining_count_fp"]) == 6, lo)
    q.cancel_all("test")
    S["positions"]["KXB-1"]["pos"] = Decimal("0")
    # per-market stop: quoter bought 100 YES of KXA-1 at .62 today, mid now .51 -> down $11 >= 10% of $100
    db.execute("INSERT INTO fills(env,fill_key,ticker,ts,yes_price,count,fee,book_side,strategy,order_source) VALUES(?,?,?,?,?,?,?,?,?,?)",
               ("demo", "q2", "KXA-1", time.time() - 60, "0.6200", "100", "0", "bid", "liquidity", "quoter"))
    q.selected = [{"ticker": "KXA-1", "daily_reward": Decimal(10), "est_daily": Decimal(1), "share": Decimal("0.05"), "capital": Decimal(10)}]
    eng.positions_cache["ts"] = 0
    q.last_sweep = 0
    q.cycle()
    lo = [o for o in S["orders"].values() if o["status"] == "resting"]
    check("per-market loss stop: no new quotes in that market for the rest of the day", lo == [] and q.mkt_stop.get("KXA-1", 0) > time.time()
          and db.query("SELECT 1 FROM audit WHERE kind='quoter_market_stop'"), (lo, q.mkt_stop))
    S["positions"]["KXA-1"] = {"pos": Decimal("5"), "exp": Decimal("3"), "fees": Decimal("0")}
    eng.positions_cache["ts"] = 0
    q.cycle()
    lo = [o for o in S["orders"].values() if o["status"] == "resting"]
    check("...but it still exits what it holds there", len(lo) == 1 and lo[0]["book_side"] == "ask" and float(lo[0]["remaining_count_fp"]) == 5, lo)
    cfg.update({"quoter": {"enabled": False}})
    q.cycle()
    S["positions"]["KXA-1"]["pos"] = Decimal("0")

    # ---- nightly backup ----
    mt = app.state.maint
    check("backup not due before the set time, due after", not mt.due(time.mktime((2026, 11, 12, 1, 0, 0, 0, 0, -1))) and mt.due(time.mktime((2026, 11, 12, 4, 0, 0, 0, 0, -1))))
    wdb = app.state.wdb
    wdb.execute("INSERT INTO mm_mids VALUES(1,'X',?,0.5,0.4,0.6)", (time.time() - 40 * 86400,))
    wdb.execute("INSERT INTO mm_mids VALUES(1,'X',?,0.5,0.4,0.6)", (time.time() - 86400,))
    res = mt.run()
    import sqlite3 as _sq
    day = res["day"]
    bk = mt.dir / f"kalshi_desk-{day}.db"
    _c = _sq.connect(str(bk))
    n = _c.execute("SELECT COUNT(*) FROM orders_log").fetchone()[0]
    _c.close()                                    # Windows keeps an open database file locked
    check("backup: ledger copy is a readable database with the orders in it; settings and research data copied",
          n == db.query("SELECT COUNT(*) n FROM orders_log")[0]["n"] and (mt.dir / f"config-{day}.json").exists()
          and (mt.dir / f"warehouse-{day}.db").exists(), res)
    check("pruning: paper-test price history older than 30 days removed, recent kept",
          res["pruned"].get("mm_mids") == 1 and wdb.query("SELECT COUNT(*) n FROM mm_mids WHERE ticker='X'")[0]["n"] == 1, res["pruned"])
    for i in range(1, 12):
        (mt.dir / f"kalshi_desk-202601{i:02d}.db").write_text("x")
    mt.run()
    check("only the last 7 ledger backups are kept", len(list(mt.dir.glob("kalshi_desk-*.db"))) == 7)
    import datetime as _dt
    late = _dt.datetime.now().replace(hour=23, minute=59).timestamp()
    check("not due again the same day", mt.due(late) is False)
    check("keys folder is never backed up", not any("key" in p.name.lower() for p in mt.dir.glob("*")))
    j = c.get("/api/maintenance").json()
    check("maintenance status endpoint", "disk_free_gb" in j["sizes"] and len(j["backups"]) >= 3)
    pf = c.get("/api/portfolio").json()
    check("portfolio rows carry their exit rule", all("exit" in p for p in pf["positions"]))
    check("exits endpoints need the CSRF token", c.post("/api/exits", json={"ticker": "KXC-1", "stop_loss": 0.5}).status_code in (401, 403))


if __name__ == "__main__":
    unit_tests()
    api_tests(tempfile.mkdtemp())
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)
