"""Read-only production history: the arithmetic on a hand-built set of fills, and proof that it can only read."""
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi import FastAPI, Request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import history as H  # noqa: E402
from app.config import Config  # noqa: E402

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def close(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


def fill(t, action, side, yes_px, n, fee, ts, taker=True):
    return {"ticker": t, "action": action, "side": side, "yes_price_dollars": f"{yes_px:.4f}", "count_fp": f"{n:.2f}",
            "fee_cost": f"{fee:.4f}", "is_taker": taker, "created_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))}


T0 = 1_794_000_000
FILLS = [
    # A: bought 10 YES at 50c, sold at 55c five minutes later (the "take 10%" trade). fees 0.18 + 0.17
    fill("KXBTC15M-A", "buy", "yes", 0.50, 10, 0.18, T0), fill("KXBTC15M-A", "sell", "yes", 0.55, 10, 0.17, T0 + 300),
    # B: bought 10 YES at 50c, held, market resolved NO: lost the stake
    fill("KXBTC15M-B", "buy", "yes", 0.50, 10, 0.18, T0 + 1000),
    # C: bought 10 NO at 40c (YES price 60c), held, resolved NO: +6.00
    fill("KXBTC15M-C", "buy", "no", 0.60, 10, 0.17, T0 + 2000, taker=False),
    # D: bought 4 NO at 30c (YES 70c), sold 4 NO at 33c (YES 67c)
    fill("KXETH15M-D", "buy", "no", 0.70, 4, 0.06, T0 + 3000), fill("KXETH15M-D", "sell", "no", 0.67, 4, 0.06, T0 + 3100),
    # E: still open, not settled
    fill("KXETH15M-E", "buy", "yes", 0.20, 5, 0.06, T0 + 4000),
    # F: V2-style fill (book side, no action/side): bid 2 at 0.25, resolved YES: +1.50
    {"ticker": "KXOTHER-F", "book_side": "bid", "yes_price_dollars": "0.2500", "count_fp": "2.00", "fee_cost": "0.0000", "is_taker": False, "ts_ms": (T0 + 5000) * 1000},
    {"ticker": "KXOTHER-G", "count_fp": "1.00"},                       # unreadable: no price or side
]
SETTS = [{"ticker": "KXBTC15M-B", "market_result": "no"}, {"ticker": "KXBTC15M-C", "market_result": "no"}, {"ticker": "KXOTHER-F", "market_result": "yes"}]


def main():
    s = H.summarize(FILLS, SETTS)
    by = {m["ticker"]: m for m in s["markets"]}
    a = by["KXBTC15M-A"]
    check("sold before the end: 10 x (55c - 50c) - fees 0.35 = +0.15", close(a["pnl"], 0.15) and a["status"] == "sold before the end" and a["held_sec"] == 300 and a["entry_c"] == 50.0, a)
    check("held to a loss: -5.00 stake - 0.18 fee", close(by["KXBTC15M-B"]["pnl"], -5.18) and by["KXBTC15M-B"]["status"] == "held to the end", by["KXBTC15M-B"])
    c = by["KXBTC15M-C"]
    check("NO bought at 40c that won: 10 x 60c - 0.17 fee = +5.83, entry shown as 40c on the NO side", close(c["pnl"], 5.83) and c["side"] == "no" and c["entry_c"] == 40.0 and close(c["staked"], 4.0), c)
    check("NO bought at 30c and sold at 33c: 4 x 3c - 0.12 = 0.00", close(by["KXETH15M-D"]["pnl"], 0.0, 1e-9) and by["KXETH15M-D"]["status"] == "sold before the end", by["KXETH15M-D"])
    check("an unsettled open position is listed but not counted", by["KXETH15M-E"]["status"] == "open" and by["KXETH15M-E"]["pnl"] is None and s["all"]["open"] == 1)
    check("order-book style fill (bid) is read as buying YES: 2 x 75c = +1.50", close(by["KXOTHER-F"]["pnl"], 1.5), by["KXOTHER-F"])
    al = s["all"]
    check("totals: 5 finished markets, 3 wins, 1 loss, 1 flat; net = 0.15 - 5.18 + 5.83 + 0 + 1.5 = 2.30", al["markets"] == 5 and al["wins"] == 3 and al["losses"] == 1 and al["flat"] == 1
          and close(al["net"], 2.30) and close(al["fees"], 0.82) and close(al["before_fees"], 3.12), al)
    check("average win, average loss and the win rate needed to break even", close(al["avg_win"], (0.15 + 5.83 + 1.5) / 3, 1e-4) and close(al["avg_loss"], -5.18)
          and close(al["breakeven_win_pct"], round(100 * 5.18 / (5.18 + (0.15 + 5.83 + 1.5) / 3), 1)), al)
    check("unreadable fills are counted, not guessed", s["fills_unreadable"] == 1 and "KXOTHER-G" not in by)
    ser = {x["series"]: x for x in s["by_series"]}
    check("results per series", ser["KXBTC15M"]["markets"] == 3 and close(ser["KXBTC15M"]["net"], 0.80) and ser["KXETH15M"]["open"] == 1, ser)
    ex = {x["exit"]: x for x in s["by_exit"]}
    check("results by how the trade ended", ex["sold before the end"]["markets"] == 2 and close(ex["held to the end"]["net"], 2.15), ex)
    check("prefix filter", H.summarize(FILLS, SETTS, "kxeth")["all"]["markets"] == 1)
    check("taker share of fills", close(al["taker_fill_pct"], round(100 * 6 / 8, 1)), al["taker_fill_pct"])

    # ---- it can only read: a mock production API that records every request
    calls = []
    app = FastAPI()

    @app.api_route("/trade-api/v2/{rest:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def anything(rest: str, request: Request):
        calls.append((request.method, "/" + rest, bool(request.headers.get("KALSHI-ACCESS-SIGNATURE")), request.headers.get("KALSHI-ACCESS-KEY")))
        if rest == "portfolio/fills":
            return {"fills": FILLS, "cursor": ""}
        if rest == "portfolio/settlements":
            return {"settlements": SETTS, "cursor": ""}
        return {}
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8931, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    tmp = Path(tempfile.mkdtemp())
    kp = tmp / "prod.pem"
    kp.write_bytes(ed25519.Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    import json
    cp = tmp / "config.json"
    cp.write_text(json.dumps({"env": "demo", "base_url_override": "http://127.0.0.1:8931/trade-api/v2"}))
    cfg = Config(cp)
    r = H.prod_history(cfg)
    check("no production key -> a clear message, no request made", r["ok"] is False and not calls, r)
    cfg.update({"prod_key_id": "PROD-KEY", "prod_private_key_path": str(kp)})
    r = H.prod_history(cfg, days=30)
    check("history read through the production key while the desk stays on demo", r["ok"] and close(r["all"]["net"], 2.30) and cfg.env == "demo", r.get("all"))
    check("only two endpoints were called, both GET and both signed with the production key",
          {(m, p) for m, p, _, _ in calls} == {("GET", "/portfolio/fills"), ("GET", "/portfolio/settlements")} and all(sg and k == "PROD-KEY" for _, _, sg, k in calls), calls)
    c = H.ReadOnlyClient("http://127.0.0.1:8931/trade-api/v2", "PROD-KEY", str(kp))
    n0 = len(calls)
    refused = 0
    for attempt in (lambda: c.create_order("KXA-1", "bid", "0.50", 1), lambda: c.cancel_order("abc"), lambda: c.amend_order("abc", "KXA-1", "bid", "0.5", 1),
                    lambda: c.request("POST", "/portfolio/fills"), lambda: c.request("GET", "/portfolio/balance"), lambda: c.request("GET", "/portfolio/orders"),
                    lambda: c.request("DELETE", "/portfolio/orders/abc"), lambda: c.request("GET", "/portfolio/fills", body={"x": 1}), lambda: c.balance(), lambda: c.orders()):
        try:
            attempt()
        except PermissionError:
            refused += 1
        except Exception as e:
            print("unexpected", repr(e))
    check("orders, cancels, amends, balance and every other call are refused before any request leaves", refused == 10 and len(calls) == n0, (refused, calls[n0:]))
    srv.should_exit = True
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
