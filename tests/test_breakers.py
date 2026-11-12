"""Phase 3 tests: circuit breakers (per-strategy daily / 7-day loss, account drawdown), against the strict mock."""
import json
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from mock_kalshi import create_mock  # noqa: E402
from app import breakers as B  # noqa: E402

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
    f = {"yes_price": "0.6000", "count": "100", "fee": "0.50", "book_side": "bid"}
    check("long YES fill P&L = (mark - price) x n - fee", close(B.fill_pnl(f, 0.51), -9.5), B.fill_pnl(f, 0.51))
    f2 = {**f, "book_side": "ask", "fee": "0"}
    check("short YES (sell YES / buy NO) P&L = (price - mark) x n", close(B.fill_pnl(f2, 0.51), 9.0))
    f3 = {"yes_price": "0.6000", "count": "10", "fee": "0", "action": "buy", "outcome_side": "no"}
    check("no book_side: buy NO counts as short YES", close(B.fill_pnl(f3, 0.70), -1.0))
    check("unpriced market -> None", B.fill_pnl(f, None) is None)
    check("settled market marked at its result", B.market_mark({"result": "yes"}) == 1.0 and B.market_mark({"result": "no"}) == 0.0)
    check("open market marked at the mid", close(B.market_mark({"yes_bid_dollars": "0.50", "yes_ask_dollars": "0.52"}), 0.51))
    check("one-sided market falls back to last price", close(B.market_mark({"yes_bid_dollars": "0", "yes_ask_dollars": "0",
                                                                         "last_price_dollars": "0.33"}), 0.33))
    check("strategy key: app strategy, else manual / outside",
          B.strategy_key({"strategy": "liquidity"}) == "liquidity" and B.strategy_key({"order_source": "manual"}) == "manual"
          and B.strategy_key({}) == "outside")


def api_tests(tmp):
    from app.config import Config
    from app.db import DB
    from app.server import create_app
    key = ed25519.Ed25519PrivateKey.generate()
    kp = Path(tmp) / "k.key"
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    mock = create_mock({"KEY-1": key.public_key()})
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
    br = eng.breakers
    check("defaults: every limit off", cfg.data["breakers"]["strategy_daily_loss"] == 0 and cfg.data["breakers"]["account_drawdown"] == 0)

    now = time.time()

    def fill(key, ticker, ts, px, n, side, strategy=None, source=None, fee="0"):
        db.execute("INSERT INTO fills(env,fill_key,ticker,ts,yes_price,count,fee,book_side,strategy,order_source) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   ("demo", key, ticker, ts, px, n, fee, side, strategy, source))
    # KXA-1 mid .51; KXB-1 mid .56
    fill("f1", "KXA-1", now - 60, "0.6000", "100", "bid", "liquidity", "quoter", "0.50")       # -9.50
    fill("f2", "KXB-1", now - 3 * 86400, "0.7000", "50", "bid", "basket_no", "auto")           # -7.00 (7d only)
    fill("f3", "KXA-1", now - 60, "0.9000", "100", "bid", None, "manual")                       # -39 manual
    st = br.check()
    s = st["strategies"]
    check("P&L by strategy today / 7 days (marked to mid)", close(s["liquidity"]["today"], -9.5) and close(s["basket_no"]["week"], -7.0)
          and close(s["basket_no"]["today"], 0.0) and close(s["manual"]["today"], -39.0), s)
    check("limits off: nothing trips", not br.tripped() and not cfg.risk.get("kill_switch"))

    cfg.update({"quoter": {"enabled": True}, "auto": {"strategies": ["basket_no", "fair_value"]}})
    r = c.post("/api/config", headers=H, json={"breakers": {"strategy_daily_loss": 5, "strategy_7d_loss": 6, "tripped": {"x": 1},
                                                            "peak_reset_ts": 99}})
    check("limits saved; 'tripped' / peak reset can't be set through config", r.status_code == 200
          and cfg.data["breakers"]["strategy_daily_loss"] == 5 and "x" not in cfg.data["breakers"]["tripped"]
          and cfg.data["breakers"]["peak_reset_ts"] == 0, r.text)
    br.check()
    tr = br.tripped()
    check("quoter breaker trips on today's loss", "liquidity" in tr and "9.50 today" in tr["liquidity"]["reason"], tr)
    check("tripped quoter is switched off", cfg.quoter["enabled"] is False)
    check("auto strategy breaker trips on the 7-day loss and is removed from auto-trading",
          "basket_no" in tr and cfg.auto["strategies"] == ["fair_value"], (tr, cfg.auto["strategies"]))
    check("manual fills never trip anything", "manual" not in tr)
    aud = [a["message"] for a in db.query("SELECT message FROM audit WHERE kind='BREAKER'")]
    check("each trip audited (-> phone alert)", len(aud) == 2, aud)
    alerts = []
    eng.alerts.send = lambda kind, title, msg, prio=0, sync=False: alerts.append((kind, prio))
    eng.alerts.on_audit("BREAKER", "liquidity turned off - test")
    check("breaker alert is high priority", alerts == [("breaker", 1)], alerts)
    br.check()
    check("a tripped breaker doesn't re-trip / re-alert", len(db.query("SELECT 1 FROM audit WHERE kind='BREAKER'")) == 2)

    r = c.post("/api/config", headers=H, json={"quoter": {"enabled": True}})
    check("can't switch the quoter back on while its breaker is tripped", r.status_code == 400 and cfg.quoter["enabled"] is False, r.text)
    r = c.post("/api/config", headers=H, json={"auto": {"strategies": ["fair_value", "basket_no"]}})
    check("can't re-add a tripped auto strategy", r.status_code == 400 and cfg.auto["strategies"] == ["fair_value"], r.text)
    check("reset refuses an untripped breaker", c.post("/api/breakers/reset", headers=H, json={"strategy": "nope"}).status_code == 400)
    r = c.post("/api/breakers/reset", headers=H, json={"strategy": "liquidity"})
    check("reset clears that breaker only", r.status_code == 200 and "liquidity" not in br.tripped() and "basket_no" in br.tripped())
    r = c.post("/api/config", headers=H, json={"quoter": {"enabled": True}})
    check("after reset the quoter can be switched on", r.status_code == 200 and cfg.quoter["enabled"] is True, r.text)
    cfg.update({"quoter": {"enabled": False}})

    # account drawdown: mock equity = $1000 cash + $25 positions = $1025
    a = br.account(time.time())
    check("account equity from Kalshi balance", close(a["equity"], 1025.0) and close(a["drawdown"], 0.0), a)
    db.execute("INSERT INTO breaker_equity2 VALUES(?,?,?,?)", ("demo", time.time() - 86400, 1200.0, 1200.0))
    a = br.account(time.time())
    check("drawdown vs the recorded peak, limit off -> no trip", close(a["drawdown"], 175.0) and not cfg.risk.get("kill_switch"), a)
    c.post("/api/config", headers=H, json={"breakers": {"account_drawdown": 100}})
    a = br.account(time.time())
    check("drawdown >= limit trips the kill switch", cfg.risk.get("kill_switch") is True and a.get("tripped"), a)
    kill = db.query("SELECT message FROM audit WHERE kind='KILL' ORDER BY id DESC LIMIT 1")
    check("kill reason names the drawdown", kill and "drawdown breaker" in kill[0]["message"], kill)
    old = db.query("SELECT 1 FROM breaker_equity2 WHERE equity=1200")
    db.execute("DELETE FROM audit WHERE kind='x'")
    c.post("/api/breakers/reset", headers=H, json={"peak": True})
    cfg.update({"risk": {"kill_switch": False}})
    time.sleep(0.01)
    a = br.account(time.time())
    check("peak reset: older peaks ignored", old and close(a["peak"], 1025.0) and not cfg.risk.get("kill_switch"), a)
    j = c.get("/api/breakers").json()
    check("status endpoint", "limits" in j and j["limits"]["account_drawdown"] == 100 and "basket_no" in j["tripped"])
    check("check endpoint needs the CSRF token", c.post("/api/breakers/check").status_code in (401, 403))
    check("check endpoint", c.post("/api/breakers/check", headers=H).status_code == 200)
    check("settings page has the breaker card", "Circuit breakers" in c.get("/").text)

    # tax export. Fills shaped exactly like Kalshi's (seen on the demo account): everything is on the YES book, and a
    # YES sale arrives labelled action=sell, side=no - so only book_side may be trusted.
    def kf(day, bs, yp, n, fee="0.0000", taker=True):
        lab = {"bid": ("buy", "yes"), "ask": ("sell", "no")}[bs]
        return {"ticker": "KXA-1", "market_ticker": "KXA-1", "book_side": bs, "action": lab[0], "side": lab[1], "outcome_side": lab[1],
                "count_fp": f"{n:.2f}", "yes_price_dollars": f"{yp:.4f}", "no_price_dollars": f"{1 - yp:.4f}", "fee_cost": fee,
                "is_taker": taker, "created_time": day + "T12:00:00Z"}
    mock.state.S["fills"] = [kf("2026-03-03", "ask", 0.30, 8, "0.0200"),       # closes the last 6 YES, opens 2 NO
                             kf("2026-03-02", "ask", 0.30, 5, taker=False),     # sells 5 of the 11 YES
                             kf("2026-03-01", "bid", 0.40, 10, "0.1700"),      # buys 10 YES (1 already held from 2025)
                             kf("2025-12-31", "bid", 0.50, 1, "0.0100")]
    r = c.get("/api/export/tax?year=2026")
    lines = r.text.strip().splitlines()
    check("tax export: CSV download named by env and year", r.status_code == 200 and "text/csv" in r.headers["content-type"]
          and 'kalshi-desk-demo-2026.csv' in r.headers.get("content-disposition", ""), r.headers)
    body = "\n".join(lines)
    check("tax export: only rows inside the year (earlier fills still set the starting position)", "2025-12-31" not in body and body.count(",fill,") == 4, body)
    check("tax export: buying YES = -price x n, fee separate", ",fill,KXA-1,buy YES (taker),10.0,0.4,-4.0,0.17" in body, body)
    check("tax export: an ask while holding YES is a SALE of YES at the YES price (not 'sell no' at 0.70)",
          ",fill,KXA-1,sell YES to close (maker),5.0,0.3,1.5,0.0" in body, body)
    check("tax export: a fill that closes and flips is split - 6 sold, 2 NO bought at 0.70; fee counted once",
          ",fill,KXA-1,sell YES to close (taker),6.0,0.3,1.8,0.02" in body and ",fill,KXA-1,buy NO (taker),2.0,0.7,-1.4,0" in body, body)
    check("tax export: settlement revenue and reward credit included", ",settlement,KXOLD-1,settled yes,,,10.0,0.17" in body
          and ",credit,,liquidity_incentive: Liquidity incentive KXA-1,,,3.21,0" in body, body)
    check("tax export: total = cash flow - fees", "net 10.75" in lines[-1] and ",11.11,0.36" in lines[-1], lines[-1])
    from app.alerts import Alerter
    check("fill alert describes the YES book, not Kalshi's 'sell no' label",
          Alerter.fill_line(kf("2026-03-02", "ask", 0.33, 1, "0.0155")) == "1 x KXA-1: SOLD YES / bought NO @ YES $0.33 (NO $0.67) (fee $0.02, taker)",
          Alerter.fill_line(kf("2026-03-02", "ask", 0.33, 1, "0.0155")))

def config_tests(tmp):
    from app.config import Config
    p = Path(tmp) / "c" / "config.json"
    p.parent.mkdir()
    cfg = Config(p)
    cfg.update({"risk": {"max_order_cost": 7}})
    cfg.update({"risk": {"max_order_cost": 9}})
    bak = p.with_suffix(".json.bak")
    check("config save is atomic: no temp file left, previous good copy kept as .bak",
          not p.with_suffix(".json.tmp").exists() and json.loads(bak.read_text())["risk"]["max_order_cost"] == 7
          and json.loads(p.read_text())["risk"]["max_order_cost"] == 9)
    p.write_text('{"env": "demo", "risk": {"max_ord')          # simulate a file cut off mid-write
    c2 = Config(p)
    check("damaged config: restored from the backup instead of silently resetting", c2.data["risk"]["max_order_cost"] == 7
          and "restored" in (c2.recovered or "") and p.with_suffix(".json.damaged").exists(), c2.recovered)
    check("restored config is written back as valid JSON", json.loads(p.read_text())["risk"]["max_order_cost"] == 7)
    p.write_text("")
    bak.unlink()
    c3 = Config(p)
    check("damaged config with no backup: safe defaults (demo, quoter and auto off)", c3.env == "demo" and not c3.quoter["enabled"]
          and not c3.auto["enabled"] and "defaults" in c3.recovered, c3.recovered)


if __name__ == "__main__":
    config_tests(tempfile.mkdtemp())
    unit_tests()
    api_tests(tempfile.mkdtemp())
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)
