"""Phase 3: yearly P&L export (CSV) for taxes, straight from Kalshi's own records for the current environment:
every fill, every settlement and every credit (liquidity rewards etc.) with a timestamp inside the year (UTC).

cash flow per row:  buy  -> -(price of the side bought x contracts)
                    sell -> +(price of the side sold   x contracts)
                    settlement -> +revenue        credit -> +amount
Fees are listed separately. Net = cash flows - fees = realized result for the year; positions still open at year end
are shown at cost (not marked). This is a worksheet for your accountant, not tax advice; check it against Kalshi's
own year-end statements.
"""
from __future__ import annotations

import calendar
import csv
import datetime as dt
import io

from .warehouse import iso_ts

COLS = ["date_utc", "type", "ticker", "description", "contracts", "price", "cash_flow", "fee"]


def _f(x) -> float:
    try:
        return float(x) if x not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _ts(obj, *keys) -> float | None:
    for k in keys:
        v = obj.get(k)
        if v in (None, ""):
            continue
        if isinstance(v, (int, float)):
            return float(v) / (1000 if v > 1e11 else 1)
        t = iso_ts(v)
        if t:
            return t
    return None


def fill_row(f: dict) -> dict:
    action = (f.get("action") or "").lower()
    side = (f.get("side") or f.get("outcome_side") or f.get("purchased_side") or "").lower()
    yp = _f(f.get("yes_price_dollars"))
    if not action or not side:                  # V2 fills: derive from the YES book side
        bs = f.get("book_side")
        action, side = ("buy", "yes") if bs == "bid" else ("buy", "no")
    px = _f(f.get("no_price_dollars")) if side == "no" and f.get("no_price_dollars") not in (None, "") else (yp if side == "yes" else 1 - yp)
    n = _f(f.get("count_fp") or f.get("count"))
    fee = _f(f.get("fee_cost") if f.get("fee_cost") is not None else f.get("fee_cost_dollars"))
    cash = (-1 if action == "buy" else 1) * px * n
    return {"type": "fill", "ticker": f.get("ticker") or f.get("market_ticker"),
            "description": f"{action} {side}{' (taker)' if f.get('is_taker') else ' (maker)'}",
            "contracts": round(n, 2), "price": round(px, 4), "cash_flow": round(cash, 4), "fee": round(fee, 4)}


def settlement_row(s: dict) -> dict:
    rev = _f(s.get("revenue")) / 100 if s.get("revenue") is not None else _f(s.get("revenue_dollars"))
    return {"type": "settlement", "ticker": s.get("ticker"), "description": f"settled {s.get('market_result') or ''}".strip(),
            "contracts": "", "price": "", "cash_flow": round(rev, 4), "fee": round(_f(s.get("fee_cost")), 4)}


def credit_row(c: dict) -> dict:
    return {"type": "credit", "ticker": "", "description": f"{c.get('type') or ''}: {c.get('reason') or ''}".strip(": "),
            "contracts": "", "price": "", "cash_flow": round(_f(c.get("amount_dollars")), 4), "fee": 0}


def build(client, year: int) -> tuple[str, dict]:
    start = calendar.timegm((year, 1, 1, 0, 0, 0))
    end = calendar.timegm((year + 1, 1, 1, 0, 0, 0))
    rows = []
    for f in client.fills(min_ts=start, max_items=200000):
        t = _ts(f, "created_time", "ts_ms", "ts")
        if t is not None and start <= t < end:
            rows.append((t, fill_row(f)))
    for s in client.settlements(min_ts=start, max_items=200000):
        t = _ts(s, "settled_time")
        if t is not None and start <= t < end:
            rows.append((t, settlement_row(s)))
    for c in client.credit_history(min_ts=start, max_items=200000):
        t = _ts(c, "created_time")
        if t is not None and start <= t < end:
            rows.append((t, credit_row(c)))
    rows.sort(key=lambda r: r[0])
    tot = {"fills": 0, "settlements": 0, "credits": 0, "cash_flow": 0.0, "fees": 0.0}
    for _, r in rows:
        tot[r["type"] + "s"] += 1
        tot["cash_flow"] += r["cash_flow"]
        tot["fees"] += r["fee"]
    tot["net"] = round(tot["cash_flow"] - tot["fees"], 2)
    tot["cash_flow"], tot["fees"] = round(tot["cash_flow"], 2), round(tot["fees"], 2)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLS)
    w.writeheader()
    for t, r in rows:
        w.writerow({"date_utc": dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), **r})
    w.writerow({})
    w.writerow({"date_utc": "TOTAL", "type": f"{year}", "description": "cash flow - fees = net (open positions at cost)",
                "cash_flow": tot["cash_flow"], "fee": tot["fees"], "contracts": "", "price": "", "ticker": f"net {tot['net']}"})
    return buf.getvalue(), tot
