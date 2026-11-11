"""Pre-trade risk checks. Every order (manual, approved, automatic, or quoter) passes through here."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from .kalshi_client import d


class RiskError(Exception):
    pass


def start_of_today() -> float:
    now = dt.datetime.now()
    return dt.datetime(now.year, now.month, now.day).timestamp()


def today_key() -> str:
    return dt.date.today().isoformat()


def order_cost(intent: str, price) -> Decimal:
    """$ at risk per contract for an opening order. buy_yes at p risks p; buy_no at q risks q."""
    if intent in ("buy_yes", "buy_no"):
        return d(price)
    return Decimal(0)


def check(cfg, *, intent: str, price, count, market: dict | None, positions: dict | None,
          spent: dict, source: str = "manual", category: str | None = None,
          resting: list | None = None) -> Decimal:
    """Raise RiskError if the order breaks a limit. Returns $ cost of the order.
    spent = {"all": $ filled on buys today, "auto": $ filled by auto/quoter today}."""
    r = cfg.risk
    p, c = d(price), d(count)
    if r.get("kill_switch"):
        raise RiskError("Kill switch is ON - no new orders")
    if c <= 0:
        raise RiskError("Count must be positive")
    if not (Decimal("0.01") <= p <= Decimal("0.99")):
        raise RiskError("Price must be between $0.01 and $0.99")
    if c > d(r["max_contracts_per_order"]):
        raise RiskError(f"Count {c} exceeds max contracts per order ({r['max_contracts_per_order']})")
    if market is not None and market.get("status") not in ("active", "open"):
        raise RiskError(f"Market is not open (status: {market.get('status')})")
    if category and category in (r.get("blocked_categories") or []):
        raise RiskError(f"Category '{category}' is blocked in settings")

    cost = order_cost(intent, p) * c
    if intent not in ("buy_yes", "buy_no"):
        return cost  # closing orders: allowed even past limits so you can always exit

    if not (d(r["min_price"]) <= p <= d(r["max_price"])):
        raise RiskError(f"Buy price ${p} outside allowed range ${r['min_price']}-${r['max_price']}")
    lim = d(r["max_order_cost"])
    if source in ("auto", "quoter"):
        lim = min(lim, d(cfg.auto["max_order_cost"]))
    if cost > lim:
        raise RiskError(f"Order cost ${cost:.2f} exceeds per-order limit ${lim:.2f}")

    s_all, s_auto = d(spent.get("all", 0)), d(spent.get("auto", 0))
    if s_all + cost > d(r["max_daily_spend"]):
        raise RiskError(f"Daily spend limit: ${s_all:.2f} filled today + ${cost:.2f} > ${r['max_daily_spend']}")
    if source in ("auto", "quoter") and s_auto + cost > d(cfg.auto["max_daily_spend"]):
        raise RiskError(f"Auto daily limit: ${s_auto:.2f} + ${cost:.2f} > ${cfg.auto['max_daily_spend']}")

    if positions is not None and market is not None:
        mps = positions.get("market_positions", [])
        # resting orders could all fill: count their worst-case cost too (None = caller has its own cap, e.g. the quoter)
        rest_mkt = sum((resting_cost(o) for o in resting or [] if o.get("ticker") == market.get("ticker")), Decimal(0))
        rest_all = sum((resting_cost(o) for o in resting or []), Decimal(0))
        mkt_exp = sum((abs(d(mp.get("market_exposure_dollars"))) for mp in mps
                       if mp.get("ticker") == market.get("ticker")), Decimal(0))
        if mkt_exp + rest_mkt + cost > d(r["max_market_exposure"]):
            raise RiskError(f"Market exposure ${mkt_exp:.2f} + ${rest_mkt:.2f} resting + ${cost:.2f} > ${r['max_market_exposure']}")
        tot = sum((abs(d(mp.get("market_exposure_dollars"))) for mp in mps), Decimal(0))
        if tot + rest_all + cost > d(r["max_total_exposure"]):
            raise RiskError(f"Total exposure ${tot:.2f} + ${rest_all:.2f} resting + ${cost:.2f} > ${r['max_total_exposure']}")
    return cost


def resting_cost(o: dict) -> Decimal:
    """Worst-case $ a resting order costs if it fills: a bid on the YES book buys YES at its price, an ask buys NO
    at 1 - price (an ask that only closes a YES position is over-counted: conservative)."""
    rem = d(o.get("remaining_count_fp") if o.get("remaining_count_fp") is not None else o.get("remaining_count"))
    if rem <= 0 or (o.get("status") or "resting") != "resting":
        return Decimal(0)
    yp = d(o.get("yes_price_dollars"))
    side = o.get("book_side") or ("bid" if (o.get("outcome_side") or o.get("side")) == "yes" else "ask")
    return rem * (yp if side == "bid" else Decimal(1) - yp)


def to_book(intent: str, price) -> tuple[str, Decimal]:
    """buy_yes @p -> bid p; sell_yes @p -> ask p; buy_no @q -> ask 1-q; sell_no @q -> bid 1-q."""
    p = d(price)
    return {
        "buy_yes": ("bid", p), "sell_yes": ("ask", p),
        "buy_no": ("ask", Decimal(1) - p), "sell_no": ("bid", Decimal(1) - p),
    }[intent]
