"""Phase 3: yearly P&L export (CSV) for taxes, straight from Kalshi's own records for the current environment:
every fill, every settlement and every credit (liquidity rewards etc.) with a timestamp inside the year (UTC).

cash flow per row:  buying YES or NO  -> -(price x contracts)      selling what you hold -> +(price x contracts)
                    settlement -> +revenue        credit -> +amount
A fill that both closes a position and opens the opposite one is written as two rows.
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


def fill_rows(f: dict, pos: float) -> tuple[list[dict], float]:
    """One Kalshi fill -> worksheet row(s) and the new net position (+ = YES contracts, - = NO contracts).

    Kalshi reports every fill on the YES book: book_side "bid" adds YES exposure, "ask" removes it. Its own action/side
    labels describe the other side of the book (a YES sale comes back as action=sell, side=no), so they are NOT used.
    What the cash did depends on what you held:
        bid while holding NO  -> closes NO:  + (1 - yes price) per contract      bid otherwise -> buys YES:  - yes price
        ask while holding YES -> closes YES: + yes price per contract            ask otherwise -> buys NO:   - (1 - yes price)
    (checked against a real demo account: buy YES @0.34 then sell @0.33 moved cash -0.34 and +0.33)."""
    yp = _f(f.get("yes_price_dollars"))
    n = _f(f.get("count_fp") or f.get("count"))
    fee = _f(f.get("fee_cost") if f.get("fee_cost") is not None else f.get("fee_cost_dollars"))
    bs = f.get("book_side")
    if bs not in ("bid", "ask"):                # older records without book_side: fall back to Kalshi's labels
        act, side = (f.get("action") or "buy").lower(), (f.get("side") or f.get("outcome_side") or "yes").lower()
        bs = "bid" if (act == "buy") == (side == "yes") else "ask"
    role = " (taker)" if f.get("is_taker") else " (maker)"
    tk = f.get("ticker") or f.get("market_ticker")
    parts = []
    if bs == "bid":
        close = min(n, max(0.0, -pos))
        if close > 0:
            parts.append(("sell NO to close", close, 1 - yp, +1))
        if n - close > 0:
            parts.append(("buy YES", n - close, yp, -1))
        pos += n
    else:
        close = min(n, max(0.0, pos))
        if close > 0:
            parts.append(("sell YES to close", close, yp, +1))
        if n - close > 0:
            parts.append(("buy NO", n - close, 1 - yp, -1))
        pos -= n
    rows = []
    for i, (desc, cnt, px, sign) in enumerate(parts):
        rows.append({"type": "fill", "ticker": tk, "description": desc + role, "contracts": round(cnt, 2), "price": round(px, 4),
                     "cash_flow": round(sign * px * cnt, 4), "fee": round(fee, 4) if i == 0 else 0})
    return rows, pos


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
    # Every fill ever (not just this year's) is replayed in order so each fill knows what was held at the time;
    # only rows inside the year are written. A settlement ends that market's position.
    events = []
    for f in client.fills(min_ts=None, max_items=500000):
        t = _ts(f, "created_time", "ts_ms", "ts")
        if t is not None and t < end:
            events.append((t, 0, f))
    for s in client.settlements(min_ts=None, max_items=500000):
        t = _ts(s, "settled_time")
        if t is not None and t < end:
            events.append((t, 1, s))
    events.sort(key=lambda e: (e[0], e[1]))
    pos: dict[str, float] = {}
    for t, kind, x in events:
        tk = x.get("ticker") or x.get("market_ticker")
        if kind == 0:
            out, pos[tk] = fill_rows(x, pos.get(tk, 0.0))
            if t >= start:
                rows += [(t, r) for r in out]
        else:
            pos[tk] = 0.0
            if t >= start:
                rows.append((t, settlement_row(x)))
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
