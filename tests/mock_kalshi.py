"""A strict mock of the Kalshi Trade API v2 for offline testing.
Verifies request signatures exactly as Kalshi documents them and validates
the V2 order body against the spec's required fields and formats."""
from __future__ import annotations

import base64
import re
import time
import uuid
from decimal import Decimal

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

PREFIX = "/trade-api/v2"


def mk(ticker, ev, sub, yb, ya, status="active", vol=500, prev=None, last=None, size=200):
    return {"ticker": ticker, "event_ticker": ev, "market_type": "binary", "title": f"{ev} {sub}",
            "yes_sub_title": sub, "status": status, "close_time": "2026-12-01T00:00:00Z",
            "yes_bid_dollars": f"{yb:.4f}", "yes_ask_dollars": f"{ya:.4f}",
            "no_bid_dollars": f"{1 - ya:.4f}", "no_ask_dollars": f"{1 - yb:.4f}",
            "yes_bid_size_fp": f"{size:.2f}", "yes_ask_size_fp": f"{size:.2f}",
            "last_price_dollars": f"{(last or ya):.4f}", "previous_price_dollars": f"{(prev or ya):.4f}",
            "volume_24h_fp": f"{vol:.2f}", "open_interest_fp": "1000.00", "rules_primary": "Test rules"}


def build_state():
    # Event A: 3 mutually exclusive outcomes; YES bids sum to 1.15 -> NO basket arb
    a = [mk("KXA-1", "KXA-26", "Alpha", .50, .52), mk("KXA-2", "KXA-26", "Beta", .40, .42),
         mk("KXA-3", "KXA-26", "Gamma", .25, .27)]
    # Event B: fair; asks sum > 1, bids sum < 1
    b = [mk("KXB-1", "KXB-26", "Home", .55, .57), mk("KXB-2", "KXB-26", "Away", .41, .43)]
    # Event C: single market, wide spread + big mover
    c = [mk("KXC-1", "KXC-26", "Yes", .30, .45, prev=.20, last=.38)]
    # Event D: YES asks sum to 0.90 -> YES basket (needs exhaustive)
    dd = [mk("KXD-1", "KXD-26", "X", .28, .30), mk("KXD-2", "KXD-26", "Y", .28, .30), mk("KXD-3", "KXD-26", "Z", .28, .30)]
    events = [
        {"event_ticker": "KXA-26", "series_ticker": "KXA", "title": "Who wins A?", "category": "Sports", "mutually_exclusive": True, "markets": a},
        {"event_ticker": "KXB-26", "series_ticker": "KXB", "title": "Game B", "category": "Sports", "mutually_exclusive": True, "markets": b},
        {"event_ticker": "KXC-26", "series_ticker": "KXC", "title": "Thing C", "category": "Economics", "mutually_exclusive": False, "markets": c},
        {"event_ticker": "KXD-26", "series_ticker": "KXD", "title": "Pick D", "category": "Politics", "mutually_exclusive": True, "markets": dd},
    ]
    now = time.time()
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
    incentives = [
        {"id": "inc1", "market_id": "m1", "market_ticker": "KXA-1", "incentive_type": "liquidity", "incentive_description": "LIP",
         "start_date": iso(now - 86400), "end_date": iso(now + 6 * 86400), "period_reward": 700000, "paid_out": False},
        {"id": "inc2", "market_id": "m2", "market_ticker": "KXB-1", "incentive_type": "liquidity", "incentive_description": "LIP",
         "start_date": iso(now - 86400), "end_date": iso(now + 6 * 86400), "period_reward": 21000, "paid_out": False},
    ]
    return {"events": events, "orders": {}, "fills": [], "positions": {}, "balance": 100000, "pv": 2500,
            "incentives": incentives}


def create_mock(public_keys: dict[str, object]):
    app = FastAPI()
    S = build_state()
    app.state.S = S
    app.state.calls = []
    app.state.order_hooks = []   # callables(order) - the WebSocket mock bridges these to user_order

    def emit(o):
        for h in app.state.order_hooks:
            h(dict(o))

    def markets():
        return {m["ticker"]: m for e in S["events"] for m in e["markets"]}

    def verify(request: Request):
        kid = request.headers.get("KALSHI-ACCESS-KEY")
        ts = request.headers.get("KALSHI-ACCESS-TIMESTAMP")
        sig = request.headers.get("KALSHI-ACCESS-SIGNATURE")
        if not (kid and ts and sig) or kid not in public_keys:
            return False
        if abs(int(ts) - time.time() * 1000) > 30000:
            return False
        msg = (ts + request.method + request.url.path).encode()  # path w/o query
        pk = public_keys[kid]
        try:
            raw = base64.b64decode(sig)
            if isinstance(pk, Ed25519PublicKey):
                pk.verify(raw, msg)
            else:
                pk.verify(raw, msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                          hashes.SHA256())
            return True
        except Exception:
            return False

    @app.middleware("http")
    async def auth(request: Request, call_next):
        app.state.calls.append((request.method, request.url.path, str(request.url.query)))
        if request.url.path.startswith(PREFIX + "/portfolio") and not verify(request):
            return JSONResponse({"code": "unauthorized", "message": "bad signature"}, 401)
        return await call_next(request)

    @app.get(PREFIX + "/exchange/status")
    def st():
        return {"exchange_active": True, "trading_active": True}

    @app.get(PREFIX + "/series")
    def series_list():
        return {"series": [{"ticker": t, "fee_type": "quadratic", "fee_multiplier": 1} for t in ("KXA", "KXB", "KXC", "KXD")]}

    @app.get(PREFIX + "/series/{t}")
    def series(t: str):
        return {"series": {"ticker": t, "fee_type": "quadratic", "fee_multiplier": 1}}

    @app.get(PREFIX + "/search/tags_by_categories")
    def tags():
        return {"tags_by_categories": {"Sports": ["NFL"], "Economics": [], "Politics": []}}

    @app.get(PREFIX + "/events")
    def events(status: str = "", with_nested_markets: str = "", limit: int = 200, cursor: str = ""):
        # paginate 2 per page to exercise cursors
        i = int(cursor or 0)
        page = S["events"][i:i + 2]
        out = [dict(e) if with_nested_markets == "true" else {k: v for k, v in e.items() if k != "markets"} for e in page]
        return {"events": out, "cursor": str(i + 2) if i + 2 < len(S["events"]) else ""}

    @app.get(PREFIX + "/events/{t}")
    def event(t: str):
        e = next((e for e in S["events"] if e["event_ticker"] == t), None)
        if e is None:
            e = S.get("hist_events", {}).get(t)
            if e is None:
                return JSONResponse({"code": "not_found", "message": "event"}, 404)
        return {"event": {k: v for k, v in e.items() if k != "markets"}, "markets": e.get("markets", [])}

    @app.get(PREFIX + "/markets")
    def mlist(tickers: str = "", limit: int = 100, status: str = "", mve_filter: str = ""):
        ms = markets()
        sel = [ms[t] for t in tickers.split(",") if t in ms] if tickers else list(ms.values())
        return {"markets": sel, "cursor": ""}

    @app.get(PREFIX + "/markets/orderbooks")
    def obs(request: Request):
        ts = request.query_params.getlist("tickers")
        return {"orderbooks": [{"ticker": t, "orderbook_fp": ob(t)["orderbook_fp"]} for t in ts if t in markets()]}

    @app.get(PREFIX + "/markets/trades")
    def public_trades(ticker: str = "", max_ts: int = 10 ** 12, min_ts: int = 0, limit: int = 100, cursor: str = ""):
        import calendar as _cal
        ts_ = lambda t: _cal.timegm(time.strptime(t["created_time"][:19], "%Y-%m-%dT%H:%M:%S"))
        sel = [t for t in S.get("public_trades", []) if t["ticker"] == ticker and min_ts <= ts_(t) <= max_ts]
        sel.sort(key=ts_, reverse=True)   # newest first, like Kalshi
        return {"trades": sel[:limit], "cursor": ""}

    @app.get(PREFIX + "/historical/trades")
    def hist_trades(ticker: str = "", max_ts: int = 10 ** 12, min_ts: int = 0, limit: int = 100, cursor: str = ""):
        import calendar as _cal
        ts_ = lambda t: _cal.timegm(time.strptime(t["created_time"][:19], "%Y-%m-%dT%H:%M:%S"))
        sel = [t for t in S.get("hist_trades", []) if t["ticker"] == ticker and min_ts <= ts_(t) <= max_ts]
        return {"trades": sel[:limit], "cursor": ""}

    @app.get(PREFIX + "/historical/markets/{t}")
    def hist_market(t: str):
        m = S.get("hist_markets", {}).get(t)
        return {"market": m} if m else JSONResponse({"code": "not_found", "message": "market"}, 404)

    @app.get(PREFIX + "/markets/{t}")
    def market(t: str):
        if t not in markets():
            return JSONResponse({"code": "not_found", "message": "market not found"}, 404)
        return {"market": markets()[t]}

    @app.get(PREFIX + "/markets/{t}/orderbook")
    def ob(t: str, depth: int = 10):
        m = markets()[t]
        yb, nb = Decimal(m["yes_bid_dollars"]), Decimal(m["no_bid_dollars"])
        return {"orderbook_fp": {"yes_dollars": [[f"{yb - Decimal('0.01'):.4f}", "50.00"], [f"{yb:.4f}", "200.00"]],
                                 "no_dollars": [[f"{nb - Decimal('0.01'):.4f}", "40.00"], [f"{nb:.4f}", "200.00"]]}}

    @app.get(PREFIX + "/portfolio/balance")
    def bal():
        return {"balance": S["balance"], "balance_dollars": f"{S['balance'] / 100:.4f}", "portfolio_value": S["pv"]}

    @app.get(PREFIX + "/portfolio/positions")
    def pos(limit: int = 100, count_filter: str = "", cursor: str = ""):
        mp = [{"ticker": t, "position_fp": f"{p['pos']:.2f}", "market_exposure_dollars": f"{p['exp']:.4f}",
               "realized_pnl_dollars": "0.0000", "fees_paid_dollars": f"{p['fees']:.4f}", "total_traded_dollars": "0"}
              for t, p in S["positions"].items() if p["pos"] != 0]
        return {"market_positions": mp, "event_positions": [], "cursor": ""}

    @app.get(PREFIX + "/portfolio/orders")
    def orders(status: str = "", limit: int = 100, min_ts: int = 0, cursor: str = "", ticker: str = ""):
        return {"orders": [o for o in S["orders"].values() if (not status or o["status"] == status)
                           and (not ticker or o["ticker"] == ticker)], "cursor": ""}

    @app.get(PREFIX + "/incentive_programs")
    def incentives(status: str = "", type: str = "", limit: int = 100, cursor: str = ""):
        return {"incentive_programs": S["incentives"], "next_cursor": ""}

    @app.get(PREFIX + "/portfolio/credit_history")
    def credits(min_ts: int = 0, limit: int = 100):
        return {"credits": [{"credit_id": "c1", "type": "liquidity_incentive", "status": "paid", "amount_dollars": "3.2100",
                             "reason": "Liquidity incentive KXA-1", "created_time": "2026-11-09T11:00:00Z"}], "cursor": ""}

    @app.get(PREFIX + "/portfolio/deposits")
    def deposits(limit: int = 100, cursor: str = ""):
        return {"deposits": S.get("deposits", []), "cursor": ""}

    @app.get(PREFIX + "/portfolio/withdrawals")
    def withdrawals(limit: int = 100, cursor: str = ""):
        return {"withdrawals": S.get("withdrawals", []), "cursor": ""}

    @app.get(PREFIX + "/portfolio/fills")
    def fills(limit: int = 100, min_ts: int = 0, cursor: str = ""):
        return {"fills": S["fills"], "cursor": ""}

    @app.get(PREFIX + "/portfolio/settlements")
    def settl(limit: int = 100):
        return {"settlements": [{"ticker": "KXOLD-1", "event_ticker": "KXOLD", "market_result": "yes",
                                 "yes_total_cost_dollars": "4.0000", "no_total_cost_dollars": "0.0000",
                                 "revenue": 1000, "settled_time": "2026-11-01T00:00:00Z", "fee_cost": "0.1700"}], "cursor": ""}

    @app.post(PREFIX + "/portfolio/events/orders")
    async def create(request: Request):
        b = await request.json()
        req = ["ticker", "side", "count", "price", "time_in_force", "self_trade_prevention_type"]
        missing = [k for k in req if k not in b]
        if missing:
            return JSONResponse({"code": "bad_request", "message": f"missing {missing}"}, 400)
        if b["side"] not in ("bid", "ask") or b["time_in_force"] not in ("fill_or_kill", "good_till_canceled", "immediate_or_cancel"):
            return JSONResponse({"code": "bad_request", "message": "bad enum"}, 400)
        if not re.fullmatch(r"\d+\.\d{2,4}", b["price"]) or not re.fullmatch(r"\d+(\.\d{1,2})?", b["count"]):
            return JSONResponse({"code": "bad_request", "message": "bad fixed point"}, 400)
        if b["self_trade_prevention_type"] not in ("taker_at_cross", "maker"):
            return JSONResponse({"code": "bad_request", "message": "bad stp"}, 400)
        if b.get("reduce_only") and b["time_in_force"] != "immediate_or_cancel":
            return JSONResponse({"code": "bad_request", "message": "reduce_only requires IOC"}, 400)
        m = markets().get(b["ticker"])
        if not m:
            return JSONResponse({"code": "not_found", "message": "market"}, 404)
        p, c = Decimal(b["price"]), Decimal(b["count"])
        ya, yb = Decimal(m["yes_ask_dollars"]), Decimal(m["yes_bid_dollars"])
        crosses = (b["side"] == "bid" and p >= ya) or (b["side"] == "ask" and p <= yb)
        if b.get("post_only") and crosses:
            return JSONResponse({"code": "post_only_cross", "message": "post only would cross"}, 400)
        fill = c if crosses else Decimal(0)
        if b["time_in_force"] == "fill_or_kill" and not crosses:
            fill = Decimal(0)
        remaining = Decimal(0) if b["time_in_force"] != "good_till_canceled" else c - fill
        oid = str(uuid.uuid4())
        fillpx = ya if b["side"] == "bid" else yb
        if fill:
            delta = fill if b["side"] == "bid" else -fill
            pp = S["positions"].setdefault(b["ticker"], {"pos": Decimal(0), "exp": Decimal(0), "fees": Decimal(0)})
            pp["pos"] += delta
            cost_per = fillpx if b["side"] == "bid" else (1 - fillpx)
            pp["exp"] += fill * cost_per
            S["fills"].append({"ticker": b["ticker"], "book_side": b["side"], "count_fp": f"{fill:.2f}",
                               "yes_price_dollars": f"{fillpx:.4f}", "fee_cost": "0.0200", "is_taker": True,
                               "created_time": "2026-11-10T21:00:00Z"})
        S["orders"][oid] = {"order_id": oid, "client_order_id": b.get("client_order_id"), "ticker": b["ticker"], "book_side": b["side"],
                            "outcome_side": "yes" if b["side"] == "bid" else "no",
                            "status": "resting" if remaining > 0 else "executed",
                            "yes_price_dollars": b["price"], "no_price_dollars": f"{1 - p:.4f}",
                            "fill_count_fp": f"{fill:.2f}", "remaining_count_fp": f"{remaining:.2f}",
                            "expiration_time": b.get("expiration_time"), "post_only": b.get("post_only"),
                            "created_time": "2026-11-10T21:00:00Z"}
        r = {"order_id": oid, "client_order_id": b.get("client_order_id"), "fill_count": f"{fill:.2f}",
             "remaining_count": f"{remaining:.2f}", "ts_ms": int(time.time() * 1000)}
        if fill:
            r["average_fill_price"] = f"{fillpx:.4f}"
            r["average_fee_paid"] = "0.0175"
        app.state.last_order = b
        emit(S["orders"][oid])
        return JSONResponse(r, 201)

    @app.delete(PREFIX + "/portfolio/events/orders/{oid}")
    def cancel(oid: str, market_ticker: str = ""):
        o = S["orders"].get(oid)
        if not o or o["status"] != "resting":
            return JSONResponse({"code": "not_found", "message": "order"}, 404)
        o["status"] = "canceled"
        red = o["remaining_count_fp"]; o["remaining_count_fp"] = "0.00"
        emit(o)
        return {"order_id": oid, "reduced_by": red, "ts_ms": int(time.time() * 1000)}

    return app
