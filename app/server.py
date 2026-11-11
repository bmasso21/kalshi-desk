"""Local web server. Binds to 127.0.0.1 only - nothing is exposed to your network."""
from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import __version__, risk
from .config import Config
from .db import DB
from .engine import NEVER_AUTO, Engine
from .fees import fee
from .config import BASE_URLS
from .kalshi_client import KalshiClient, KalshiError, book_from_orderbook, d, load_private_key

STATIC = Path(__file__).parent / "static"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def create_app(cfg: Config | None = None, db: DB | None = None, start_engine: bool = True) -> FastAPI:
    cfg = cfg or Config()
    db = db or DB()
    eng = Engine(cfg, db)
    app = FastAPI(title="Kalshi Desk", version=__version__)
    app.state.engine = eng
    session_token = secrets.token_urlsafe(24)  # CSRF guard for state-changing calls
    prod_confirms: dict[str, float] = {}

    @app.on_event("startup")
    def _start():
        if start_engine:
            eng.start()

    @app.on_event("shutdown")
    def _stop():
        eng.stop()

    @app.exception_handler(KalshiError)
    async def _kerr(_, e: KalshiError):
        return JSONResponse({"error": str(e), "detail": e.payload}, status_code=400)

    @app.exception_handler(risk.RiskError)
    async def _rerr(_, e: risk.RiskError):
        return JSONResponse({"error": f"Risk check: {e}"}, status_code=400)

    @app.middleware("http")
    async def _csrf(request: Request, call_next):
        if request.method in ("POST", "PUT", "DELETE") and request.url.path.startswith("/api/"):
            if request.headers.get("X-Desk-Token") != session_token:
                return JSONResponse({"error": "bad session token - reload the page"}, status_code=403)
        return await call_next(request)

    # ------------------------------------------------------------------ ui --
    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/session")
    def session():
        return {"token": session_token, "version": __version__}

    @app.get("/api/status")
    def status():
        c = eng.client
        fs = eng.feed.status()
        out = {"env": cfg.env, "authenticated": c.authenticated, "scan": eng.scan_state,
               "kill_switch": cfg.risk["kill_switch"], "auto": cfg.auto["enabled"],
               "feed": {k: fs[k] for k in ("mode", "connected", "reason", "error", "markets_watched",
                                           "books_synced", "since", "last_msg")}}
        try:
            out["exchange"] = c.exchange_status()
        except Exception as e:
            out["exchange_error"] = str(e)
        return out

    # ------------------------------------------------------------- config --
    @app.get("/api/config")
    def get_config():
        return {"config": cfg.public_view(), "never_auto": sorted(NEVER_AUTO)}

    @app.post("/api/config")
    async def set_config(request: Request):
        patch = await request.json()
        for k in ("demo_private_key_path", "prod_private_key_path", "demo_key_id", "prod_key_id"):
            if k in patch and (patch[k] in ("", None, True, False) or str(patch[k]).endswith("…")):
                patch.pop(k)  # don't overwrite secrets with masked values
        errors = []
        for e in ("demo", "prod"):
            kid = f"{e}_key_id"
            if kid in patch:
                patch[kid] = str(patch[kid]).strip()
                if not UUID_RE.fullmatch(patch[kid].lower()):
                    patch.pop(kid)
                    errors.append(f"{e} Key ID is not a Kalshi Key ID (expected format "
                                  f"xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx). Don't paste the private key there.")
            pk = f"{e}_private_key_path"
            if pk in patch:
                p = str(patch[pk]).strip().strip('"').strip("'")
                try:
                    load_private_key(p)
                    patch[pk] = p
                except FileNotFoundError:
                    patch.pop(pk)
                    errors.append(f"{e} key file not found: {p}")
                except Exception:
                    patch.pop(pk)
                    errors.append(f"{e} key file is not a valid private key: {p}")
        if "auto" in patch and "strategies" in patch["auto"]:
            patch["auto"]["strategies"] = [s for s in patch["auto"]["strategies"] if s not in NEVER_AUTO]
        old_env = cfg.env
        if patch.get("env") and patch["env"] != old_env:
            eng.quoter.cancel_all("environment switch")
        cfg.update(patch)
        if any(k in patch for k in ("env", "base_url_override")) or any(k.endswith(("_key_id", "_key_path")) for k in patch):
            eng.connect()
        db.audit("config", json.dumps({k: v for k, v in patch.items() if "key" not in k})[:1000])
        if cfg.env != old_env:
            eng.trigger_scan()
        out = get_config()
        out["errors"] = errors
        out["authenticated"] = eng.client.authenticated
        if errors:
            return JSONResponse({**out, "error": " | ".join(errors)}, status_code=400)
        return out

    @app.post("/api/kill")
    async def kill(request: Request):
        on = (await request.json()).get("on", True)
        if on:
            cancelled = eng.trip_kill("manual kill switch")
        else:
            cfg.update({"risk": {"kill_switch": False}})
            db.audit("kill_switch", "released")
            cancelled = []
        return {"kill_switch": bool(on), "cancelled": cancelled}

    # -------------------------------------------------------------- quoter --
    @app.get("/api/quoter")
    def quoter():
        return {**eng.quoter.snapshot(), "config": cfg.quoter, "env": cfg.env}

    @app.get("/api/quoter/preview")
    def quoter_preview(env: str = "prod", size: int = 0):
        """Read-only dry run of market selection against public data. Places nothing."""
        if env not in BASE_URLS:
            raise HTTPException(400, "env must be demo or prod")
        q = eng.quoter
        old = cfg.quoter.get("size")
        try:
            if size > 0:
                cfg.quoter["size"] = size
            rows, msg = q.select(KalshiClient(BASE_URLS[env]))
        finally:
            cfg.quoter["size"] = old
        return {"env": env, "size": size or old, "selected": q._fmt(rows), "summary": msg}

    @app.get("/api/rewards")
    def rewards(days: int = 30):
        if not eng.client.authenticated:
            return {"authenticated": False}
        credits = eng.client.credit_history(min_ts=int(time.time()) - days * 86400)
        sp = eng.spent_today(max_age=0)
        pnl = eng.daily_pnl()
        q_orders = db.query("SELECT COUNT(*) n FROM orders_log WHERE env=? AND source='quoter' AND ts>=?",
                            (cfg.env, risk.start_of_today()))[0]["n"]
        return {"authenticated": True, "credits": credits,
                "credits_total": str(sum((d(c.get("amount_dollars")) for c in credits
                                          if "liquid" in (str(c.get("reason")) + str(c.get("type"))).lower()), d(0))),
                "other_credits_total": str(sum((d(c.get("amount_dollars")) for c in credits
                                                if "liquid" not in (str(c.get("reason")) + str(c.get("type"))).lower()), d(0))),
                "spent_today": {k: str(v) for k, v in sp.items()},
                "pnl_today": {k: str(v) for k, v in pnl.items()} if pnl else None,
                "max_daily_loss": cfg.risk.get("max_daily_loss"), "quoter_orders_today": q_orders}

    # -------------------------------------------------------- market data --
    @app.get("/api/categories")
    def categories():
        return eng.client.tags_by_category()

    @app.get("/api/events")
    def events(category: str = "", q: str = "", series_ticker: str = "", limit: int = 300):
        evs = eng.client.events(status="open", series_ticker=series_ticker or None, nested=True, max_items=3000)
        if category:
            evs = [e for e in evs if e.get("category") == category]
        if q:
            ql = q.lower()
            evs = [e for e in evs if ql in (e.get("title", "") + " " + e.get("sub_title", "") + " " +
                                           e.get("event_ticker", "")).lower()
                   or any(ql in (m.get("title", "") + m.get("yes_sub_title", "")).lower() for m in e.get("markets") or [])]
        def vol(e):
            return sum(float(m.get("volume_24h_fp") or 0) for m in e.get("markets") or [])
        evs.sort(key=vol, reverse=True)
        return {"events": evs[:limit], "total": len(evs)}

    @app.get("/api/feed")
    def feed_status():
        return eng.feed.status()

    def _book(ticker: str):
        eng.feed.want_ui(ticker)
        ob = eng.feed.book(ticker, depth=10)
        if ob is not None:
            return ob, "ws"
        return eng.client.orderbook(ticker, depth=10), "rest"

    @app.get("/api/book/{ticker}")
    def live_book(ticker: str):
        """Lightweight book refresh for the market view (live feed when available)."""
        ob, src = _book(ticker)
        book = book_from_orderbook(ob)
        js = lambda lv: [[str(p), str(c)] for p, c in lv]
        top = eng.feed.top(ticker)
        return {"ticker": ticker, "source": src, "book": {k: js(v) for k, v in book.items()},
                "top": {k: str(v) for k, v in top.items()} if top else None}

    @app.get("/api/market/{ticker}")
    def market(ticker: str):
        m = eng.feed.market_overlay(eng.client.market(ticker))
        ob, src = _book(ticker)
        book = book_from_orderbook(ob)
        ser = {}
        try:
            ev = eng.client.request("GET", f"/events/{m['event_ticker']}").get("event") or {}
            ser = eng.client.series(ev.get("series_ticker")) or {}
        except Exception:
            ev = {}
        fv = db.query("SELECT * FROM fair_values WHERE ticker=?", (ticker,))
        js = lambda lv: [[str(p), str(c)] for p, c in lv]
        return {"market": m, "book_source": src,
                "event": {k: ev.get(k) for k in ("title", "category", "mutually_exclusive", "series_ticker")},
                "fee_type": ser.get("fee_type"), "fee_multiplier": ser.get("fee_multiplier"),
                "book": {k: js(v) for k, v in book.items()}, "fair_value": fv[0] if fv else None}

    @app.get("/api/history/{ticker}")
    def history(ticker: str, days: int = 7):
        m = eng.client.market(ticker)
        ev = eng.client.request("GET", f"/events/{m['event_ticker']}").get("event") or {}
        end = int(time.time())
        period = 60 if days <= 14 else 1440
        return eng.client.candlesticks(ev["series_ticker"], ticker, end - days * 86400, end, period)

    @app.get("/api/fee")
    def fee_quote(price: str, count: str, fee_type: str = "quadratic", multiplier: str = "1"):
        return {"taker": str(fee(price, count, fee_type, multiplier)),
                "maker": str(fee(price, count, fee_type, multiplier, maker=True))}

    # ---------------------------------------------------------- portfolio --
    @app.get("/api/portfolio")
    def portfolio():
        c = eng.client
        if not c.authenticated:
            return {"authenticated": False}
        bal = c.balance()
        pos = eng.positions(max_age=0)
        tickers = [p["ticker"] for p in pos["market_positions"]]
        mk = {}
        for i in range(0, len(tickers), 100):
            for m in c.markets(status=None, tickers=tickers[i:i + 100], exclude_combos=False):
                mk[m["ticker"]] = m
        rows = []
        for p in pos["market_positions"]:
            m = mk.get(p["ticker"], {})
            qty = d(p.get("position_fp"))
            side = "YES" if qty > 0 else "NO"
            mark = d(m.get("yes_bid_dollars")) if qty > 0 else d(m.get("no_bid_dollars"))
            rows.append({**p, "title": m.get("title"), "sub": m.get("yes_sub_title"), "status": m.get("status"),
                         "side": side, "qty": str(abs(qty)), "mark": str(mark),
                         "mark_value": str((abs(qty) * mark).quantize(d("0.01"))),
                         "close_time": m.get("close_time")})
        return {"authenticated": True, "balance": bal, "positions": rows,
                "orders": c.orders(status="resting"), "fills": c.fills(max_items=200)}

    @app.get("/api/settlements")
    def settlements():
        if not eng.client.authenticated:
            return {"settlements": []}
        s = eng.client.settlements(max_items=1000)
        for r in s:
            db.execute("INSERT OR REPLACE INTO settlements VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (cfg.env, r.get("ticker"), r.get("event_ticker"), r.get("settled_time"), r.get("market_result"),
                        r.get("revenue"), r.get("yes_total_cost_dollars"), r.get("no_total_cost_dollars"),
                        r.get("fee_cost"), json.dumps(r)))
        return {"settlements": s}

    # ------------------------------------------------------------- orders --
    @app.post("/api/order")
    async def order(request: Request):
        b = await request.json()
        if cfg.env == "prod" and cfg.risk.get("require_confirm_prod"):
            tok = b.get("confirm_token")
            if not tok or tok not in prod_confirms or time.time() - prod_confirms.pop(tok) > 60:
                t = secrets.token_hex(8)
                prod_confirms[t] = time.time()
                return JSONResponse({"needs_confirm": True, "confirm_token": t,
                                     "message": "REAL MONEY. Confirm this order within 60 seconds."}, status_code=409)
        r = eng.place(ticker=b["ticker"], intent=b["intent"], price=b["price"], count=b["count"],
                      time_in_force=b.get("time_in_force", "good_till_canceled"),
                      post_only=bool(b.get("post_only")), expiration_time=b.get("expiration_time"))
        return r

    @app.delete("/api/order/{order_id}")
    def cancel(order_id: str, ticker: str = ""):
        r = eng.client.cancel_order(order_id, ticker or None)
        db.audit("cancel", f"{order_id} {ticker}")
        return r

    @app.post("/api/cancel_all")
    def cancel_all():
        n = 0
        for o in eng.client.orders(status="resting"):
            eng.client.cancel_order(o["order_id"], o.get("ticker"))
            n += 1
        db.audit("cancel_all", str(n))
        return {"cancelled": n}

    # ------------------------------------------------------------ scanner --
    @app.get("/api/signals")
    def signals(status: str = "new"):
        rows = db.query("SELECT * FROM signals WHERE env=? AND status=? ORDER BY actionable DESC, CAST(edge AS REAL) DESC LIMIT 300",
                        (cfg.env, status))
        for r in rows:
            r["legs"] = json.loads(r["legs"] or "[]")
            r["max_fit"] = eng.fit_size(r["legs"]) if r["legs"] else 0
        return {"signals": rows, "scan": eng.scan_state}

    @app.post("/api/scan")
    def scan():
        eng.trigger_scan()
        return {"ok": True}

    @app.post("/api/signals/{sid}/approve")
    async def approve(sid: int, request: Request):
        b = await request.json()
        rows = db.query("SELECT * FROM signals WHERE id=? AND env=?", (sid, cfg.env))
        if not rows:
            raise HTTPException(404, "signal not found")
        if not rows[0]["actionable"]:
            raise HTTPException(400, "informational signal - use the order ticket instead")
        if cfg.env == "prod" and cfg.risk.get("require_confirm_prod"):
            tok = b.get("confirm_token")
            if not tok or tok not in prod_confirms or time.time() - prod_confirms.pop(tok) > 60:
                t = secrets.token_hex(8)
                prod_confirms[t] = time.time()
                return JSONResponse({"needs_confirm": True, "confirm_token": t,
                                     "message": "REAL MONEY. Confirm within 60 seconds."}, status_code=409)
        return eng.execute_signal(rows[0], b.get("count"), source="approved")

    @app.post("/api/signals/{sid}/dismiss")
    def dismiss(sid: int):
        db.execute("UPDATE signals SET status='dismissed' WHERE id=?", (sid,))
        return {"ok": True}

    # -------------------------------------------------------- fair values --
    @app.get("/api/fair_values")
    def fair_values():
        return {"fair_values": db.query("SELECT * FROM fair_values ORDER BY ts DESC")}

    @app.post("/api/fair_values")
    async def set_fair_value(request: Request):
        b = await request.json()
        fv = d(b["fair_yes"])
        if not (d("0.01") <= fv <= d("0.99")):
            raise HTTPException(400, "fair_yes must be between 0.01 and 0.99")
        db.execute("INSERT OR REPLACE INTO fair_values VALUES(?,?,?,?)",
                   (b["ticker"], str(fv), b.get("note", ""), time.time()))
        return {"ok": True}

    @app.delete("/api/fair_values/{ticker}")
    def del_fair_value(ticker: str):
        db.execute("DELETE FROM fair_values WHERE ticker=?", (ticker,))
        return {"ok": True}

    # ------------------------------------------------------------ ledger --
    @app.get("/api/ledger")
    def ledger(quoter: int = 0):
        where = "" if quoter else "WHERE source!='quoter'"
        return {"orders": db.query("SELECT id,ts,env,source,strategy,ticker,intent,price,count,cost,status,"
                                   f"fill_count,avg_fill_price,avg_fee,error FROM orders_log {where} ORDER BY id DESC LIMIT 500"),
                "audit": db.query("SELECT * FROM audit ORDER BY id DESC LIMIT 200"),
                "spent_today": str(eng.spent_today(max_age=0)["all"]) if eng.client.authenticated else "0"}

    return app
