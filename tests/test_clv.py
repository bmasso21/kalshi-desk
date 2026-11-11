"""Phase 1.5 tests: closing-line value tracker (hand-computed expectations)."""
import json
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

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def main():
    from app.clv import clv_sign
    from app.config import Config
    from app.db import DB
    from app.server import create_app

    tmp = tempfile.mkdtemp()
    key = ed25519.Ed25519PrivateKey.generate()
    kp = Path(tmp) / "k.key"
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    rest = create_mock({"K": key.public_key()})
    srv = uvicorn.Server(uvicorn.Config(rest, host="127.0.0.1", port=8981, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    cfgp = Path(tmp) / "config.json"
    cfgp.write_text(json.dumps({"env": "demo", "base_url_override": "http://127.0.0.1:8981/trade-api/v2", "ws_enabled": False,
                                "demo_key_id": "K", "demo_private_key_path": str(kp)}))
    cfg = Config(cfgp)
    db = DB(Path(tmp) / "t.db")
    app = create_app(cfg, db, start_engine=False)
    eng = app.state.engine
    c = TestClient(app)
    H = {"X-Desk-Token": c.get("/api/session").json()["token"]}
    S = rest.state.S
    now = time.time()
    close = int(now - 3600)

    # sign rules
    check("sign: book_side bid=+1, ask=-1", clv_sign(None, None, "bid") == 1 and clv_sign(None, None, "ask") == -1)
    check("sign fallback: buy yes +1, buy no -1, sell yes -1, sell no +1",
          (clv_sign("buy", "yes"), clv_sign("buy", "no"), clv_sign("sell", "yes"), clv_sign("sell", "no")) == (1, -1, -1, 1))

    # market KXB-1 closed an hour ago; last trade before close .62; a trade after close must be ignored
    mb = S["events"][1]["markets"][0]
    mb.update(status="closed", close_time=iso(close))
    S["public_trades"] = [
        {"trade_id": "p1", "ticker": "KXB-1", "created_time": iso(close - 7200), "yes_price_dollars": "0.5500", "count_fp": "5.00"},
        {"trade_id": "p2", "ticker": "KXB-1", "created_time": iso(close - 60), "yes_price_dollars": "0.6200", "count_fp": "3.00"},
        {"trade_id": "p3", "ticker": "KXB-1", "created_time": iso(close + 600), "yes_price_dollars": "0.9900", "count_fp": "9.00"}]
    db.log_order(env="demo", source="approved", strategy="fair_value", ticker="KXB-1", intent="buy_yes", price="0.57",
                 count="10", status="placed", order_id="O1")

    def fill(tid, oid, t, yp, n, book_side=None, action=None, side=None):
        f = {"trade_id": tid, "order_id": oid, "market_ticker": t, "yes_price_dollars": f"{yp:.4f}", "count_fp": f"{n:.2f}",
             "ts_ms": int((close - 3 * 3600) * 1000)}
        if book_side:
            f["book_side"] = book_side
        if action:
            f["action"] = action
        if side:
            f["purchased_side"] = side
        db.record_fill("demo", f, "ws")

    fill("F1", "O1", "KXB-1", 0.57, 10, "bid", "buy", "yes")     # +5
    fill("F2", "O2", "KXB-1", 0.60, 5, "ask", "buy", "no")       # -2
    fill("F3", "O3", "KXB-1", 0.65, 4, "ask", "sell", "yes")     # +3
    fill("F4", "O4", "KXB-1", 0.70, 2, None, "sell", "no")       # -8 (fallback sign)
    fill("F5", "O5", "KXA-1", 0.50, 7, "bid", "buy", "yes")      # market still open -> pending
    r = eng.clv.run_once()
    rows = {x["fill_key"].split(":")[0]: x for x in db.query("SELECT * FROM clv")}
    check("4 fills scored, 1 pending", r == {"scored": 4, "pending": 1}, r)
    check("closing line = last trade BEFORE close (.62, not the .99 after)", rows["F1"]["closing_yes"] == 0.62 and rows["F1"]["method"] == "last_trade")
    check("buy YES 10 @ .57 -> +5c", rows["F1"]["clv_cents"] == 5.0, rows["F1"]["clv_cents"])
    check("buy NO (yes .60) -> -2c", rows["F2"]["clv_cents"] == -2.0, rows["F2"]["clv_cents"])
    check("sell YES @ .65 -> +3c", rows["F3"]["clv_cents"] == 3.0, rows["F3"]["clv_cents"])
    check("sell NO (yes .70, no book_side) -> -8c", rows["F4"]["clv_cents"] == -8.0, rows["F4"]["clv_cents"])
    check("open market stays pending", rows["F5"]["status"] == "pending" and rows["F5"]["clv_cents"] is None)
    check("category/series from the event", rows["F1"]["category"] == "Sports" and rows["F1"]["series"] == "KXB")
    s = c.get("/api/clv?days=30").json()
    o = s["overall"]
    # (10*5 + 5*-2 + 4*3 + 2*-8) / 21 = 36/21 = 1.714
    check("overall contract-weighted CLV 1.71c over 21 contracts", o["avg_clv_cents"] == 1.71 and o["contracts"] == 21 and o["fills"] == 4, o)
    check("total CLV $0.36, beat close 66.7%", o["total_clv_dollars"] == 0.36 and o["beat_close_pct"] == 66.7, o)
    check("by strategy: fair_value +5c, outside (36-50)/11", s["by_strategy"]["fair_value"]["avg_clv_cents"] == 5.0
          and s["by_strategy"]["outside"]["avg_clv_cents"] == round(-14 / 11, 2), s["by_strategy"])
    check("by category + series", s["by_category"]["Sports"]["contracts"] == 21 and s["by_series"]["KXB"]["fills"] == 4)
    check("pending counted", s["pending"] == 1)
    check("strategy_clv for the allocator", eng.clv.strategy_clv("fair_value")["avg_clv_cents"] == 5.0)

    # pending -> scored once the market closes; no trades -> market last price fallback
    ma = S["events"][0]["markets"][0]
    ma.update(status="closed", close_time=iso(close), last_price_dollars="0.5300")
    eng.clv.markets.clear()
    r = eng.clv.run_once()
    row = db.query("SELECT * FROM clv WHERE ticker='KXA-1'")[0]
    check("pending fill scored after close (fallback: market last price)", r["scored"] == 1 and row["status"] == "done"
          and row["closing_yes"] == 0.53 and row["method"] == "market_last_price" and row["clv_cents"] == 3.0, row)
    r = eng.clv.run_once()
    check("already-scored fills not re-scored", r == {"scored": 0, "pending": 0}, r)

    # no closing data at all -> 'nodata' (not counted)
    mc = S["events"][2]["markets"][0]
    mc.update(status="closed", close_time=iso(close), last_price_dollars="0.0000")
    fill("F6", "O6", "KXC-1", 0.30, 3, "bid")
    eng.clv.run_once()
    check("no trades + no last price -> nodata, excluded from averages",
          db.query("SELECT status FROM clv WHERE ticker='KXC-1'")[0]["status"] == "nodata"
          and c.get("/api/clv").json()["overall"]["contracts"] == 28)

    # archived market: /markets/{t} 404 -> /historical/markets + /historical/trades
    S["hist_markets"] = {"KXOLD-9": {"ticker": "KXOLD-9", "event_ticker": "KXOLD", "status": "finalized",
                                     "close_time": iso(close - 86400 * 60), "last_price_dollars": "0.4000"}}
    S["hist_events"] = {"KXOLD": {"event_ticker": "KXOLD", "series_ticker": "KXOLDS", "category": "Economics"}}
    S["hist_trades"] = [{"trade_id": "h1", "ticker": "KXOLD-9", "created_time": iso(close - 86400 * 60 - 30), "yes_price_dollars": "0.8100"}]
    fill("F7", "O7", "KXOLD-9", 0.75, 2, "bid")
    eng.clv.run_once()
    row = db.query("SELECT * FROM clv WHERE ticker='KXOLD-9'")[0]
    check("archived market via /historical endpoints: CL .81 -> +6c", row["closing_yes"] == 0.81 and row["clv_cents"] == 6.0
          and row["category"] == "Economics", row)

    # window filter + daily summary line + UI endpoint
    db.execute("UPDATE clv SET ts=? WHERE ticker='KXOLD-9'", (now - 40 * 86400,))
    check("30-day window excludes older fills", c.get("/api/clv?days=30").json()["overall"]["contracts"] == 28
          and c.get("/api/clv?days=90").json()["overall"]["contracts"] == 30)
    txt = eng.alerts.summary_text()
    check("daily summary includes 30d CLV", "CLV 30d:" in txt, txt)
    check("manual score endpoint (CSRF protected)", c.post("/api/clv/run").status_code == 403
          and c.post("/api/clv/run", headers=H).status_code == 200)
    check("CLV tracker never places orders", not db.query("SELECT 1 FROM orders_log WHERE order_id!='O1'"))


if __name__ == "__main__":
    main()
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)
