"""Scanner strategies. Each returns a list of Signal dicts.

Signal = {strategy, key, title, detail, edge (str $ per unit), actionable (bool),
          legs: [{ticker, intent, price, count, title}], category, warning}

Strategies
----------
basket_no   Mutually exclusive event: buy NO on every outcome. At most one outcome can
            resolve YES, so the basket pays at least N-1 dollars. Profit if
            sum(NO asks) + fees < N-1. Risk-free except leg risk (one leg fills, another
            doesn't) and rule disputes.
basket_yes  Mutually exclusive event: buy YES on every outcome. Pays $1 ONLY if exactly
            one outcome resolves YES. If the event can end with no listed outcome winning
            (e.g. no "Other"/"None" market), the whole basket can lose. Never auto-traded.
fair_value  You enter a fair YES probability for a market (your model, a sportsbook
            no-vig line, etc). Flags when the ask beats your number by min_edge after fees.
wide_spread Liquid market with a wide bid/ask spread: a post-only limit inside the spread
            earns the spread (maker fees are 0 on most series). Informational.
movers      Big move since the previous day's close. Informational.
"""
from __future__ import annotations

import time
from decimal import Decimal

from .fees import fee_per_contract
from .kalshi_client import best_quotes, d

ONE = Decimal(1)


def _active(m):
    return m.get("status") == "active" and (m.get("market_type") or "binary") == "binary"


def _fee_info(series_fees: dict, event: dict):
    s = series_fees.get(event.get("series_ticker"), {})
    return s.get("fee_type") or "quadratic", s.get("fee_multiplier") or 1


def basket_no(events, series_fees, cfg):
    out = []
    min_edge = d(cfg.scanner["min_edge"])
    for ev in events:
        ms = ev.get("markets") or []
        if not ev.get("mutually_exclusive") or len(ms) < 2 or not all(_active(m) for m in ms):
            continue
        ft, mult = _fee_info(series_fees, ev)
        legs, cost, fees, size = [], Decimal(0), Decimal(0), None
        ok = True
        for m in ms:
            q = best_quotes(m)
            no_ask = q["no_ask"]
            avail = d(m.get("yes_bid_size_fp"))  # selling YES into YES bids == buying NO
            if no_ask <= 0 or no_ask >= 1 or avail <= 0:
                ok = False
                break
            cost += no_ask
            fees += fee_per_contract(no_ask, ft, mult)
            size = avail if size is None else min(size, avail)
            legs.append({"ticker": m["ticker"], "intent": "buy_no", "price": str(no_ask),
                         "title": m.get("yes_sub_title") or m.get("title")})
        if not ok:
            continue
        n = len(ms)
        edge = Decimal(n - 1) - cost - fees
        if edge >= min_edge:
            cnt = int(min(size, Decimal(100)))
            for l in legs:
                l["count"] = cnt
            out.append({
                "strategy": "basket_no", "key": f"basket_no:{ev['event_ticker']}",
                "title": ev.get("title"), "category": ev.get("category"),
                "detail": f"Buy NO on all {n} outcomes for ${cost:.2f} + ~${fees:.2f} fees; pays >= ${n - 1}. "
                          f"Depth supports ~{cnt} baskets.",
                "edge": str(edge.quantize(Decimal('0.0001'))), "actionable": True, "legs": legs,
                "warning": "Leg risk: if one leg fails to fill the rest are unhedged. Orders are fill-or-kill per leg.",
            })
    return out


def basket_yes(events, series_fees, cfg):
    out = []
    min_edge = d(cfg.scanner["min_edge"])
    for ev in events:
        ms = ev.get("markets") or []
        if not ev.get("mutually_exclusive") or len(ms) < 2 or not all(_active(m) for m in ms):
            continue
        ft, mult = _fee_info(series_fees, ev)
        legs, cost, fees, size, ok = [], Decimal(0), Decimal(0), None, True
        for m in ms:
            ya = best_quotes(m)["yes_ask"]
            avail = d(m.get("yes_ask_size_fp"))
            if ya <= 0 or ya >= 1 or avail <= 0:
                ok = False
                break
            cost += ya
            fees += fee_per_contract(ya, ft, mult)
            size = avail if size is None else min(size, avail)
            legs.append({"ticker": m["ticker"], "intent": "buy_yes", "price": str(ya),
                         "title": m.get("yes_sub_title") or m.get("title")})
        if not ok:
            continue
        edge = ONE - cost - fees
        if edge >= min_edge:
            cnt = int(min(size, Decimal(100)))
            for l in legs:
                l["count"] = cnt
            out.append({
                "strategy": "basket_yes", "key": f"basket_yes:{ev['event_ticker']}",
                "title": ev.get("title"), "category": ev.get("category"),
                "detail": f"Buy YES on all {len(ms)} outcomes for ${cost:.2f} + ~${fees:.2f} fees; pays $1 only if "
                          f"exactly one outcome resolves YES.",
                "edge": str(edge.quantize(Decimal('0.0001'))), "actionable": True, "legs": legs,
                "warning": "READ THE RULES FIRST: if no listed outcome can win, this basket loses everything. "
                           "Never auto-traded.",
            })
    return out


def fair_value(markets_by_ticker, fair_values, series_fees, event_series, cfg):
    out = []
    min_edge = d(cfg.scanner["min_edge"])
    for fv in fair_values:
        m = markets_by_ticker.get(fv["ticker"])
        if not m or not _active(m):
            continue
        s = series_fees.get(event_series.get(m.get("event_ticker")), {})
        ft, mult = s.get("fee_type") or "quadratic", s.get("fee_multiplier") or 1
        fair = d(fv["fair_yes"])
        q = best_quotes(m)
        for intent, ask, value, size_key in (("buy_yes", q["yes_ask"], fair, "yes_ask_size_fp"),
                                             ("buy_no", q["no_ask"], ONE - fair, "yes_bid_size_fp")):
            if ask <= 0 or ask >= 1:
                continue
            edge = value - ask - fee_per_contract(ask, ft, mult)
            if edge >= min_edge:
                out.append({
                    "strategy": "fair_value", "key": f"fair_value:{m['ticker']}:{intent}",
                    "title": m.get("title"), "category": None,
                    "detail": f"{intent.replace('_', ' ').upper()} at ${ask:.2f}; your fair value "
                              f"{'YES' if intent == 'buy_yes' else 'NO'} = ${value:.2f}. Note: {fv.get('note') or '-'}",
                    "edge": str(edge.quantize(Decimal('0.0001'))), "actionable": True,
                    "legs": [{"ticker": m["ticker"], "intent": intent, "price": str(ask),
                              "count": int(min(d(m.get(size_key)), Decimal(50))) or 1, "title": m.get("title")}],
                    "warning": "Edge is only as good as your fair value.",
                })
    return out


def wide_spread(markets, cfg):
    out = []
    minvol = d(cfg.scanner["min_volume_24h"])
    for m in markets:
        q = best_quotes(m)
        if q["yes_bid"] <= 0 or q["yes_ask"] <= 0:
            continue
        spread = q["yes_ask"] - q["yes_bid"]
        if spread >= Decimal("0.08") and d(m.get("volume_24h_fp")) >= minvol:
            out.append({
                "strategy": "wide_spread", "key": f"wide_spread:{m['ticker']}",
                "title": m.get("title"), "category": None,
                "detail": f"YES bid ${q['yes_bid']:.2f} / ask ${q['yes_ask']:.2f} (spread ${spread:.2f}), "
                          f"24h vol {d(m.get('volume_24h_fp')):.0f}. A post-only quote inside the spread earns it if filled.",
                "edge": str((spread / 2).quantize(Decimal('0.0001'))), "actionable": False,
                "legs": [{"ticker": m["ticker"], "intent": "buy_yes", "price": str(q["yes_bid"] + Decimal('0.01')),
                          "count": 10, "title": m.get("title")}],
                "warning": "Not an arbitrage. You get filled when someone disagrees with you.",
            })
    return out


def movers(markets, cfg):
    out = []
    minvol = d(cfg.scanner["min_volume_24h"])
    for m in markets:
        q = best_quotes(m)
        if q["prev"] <= 0 or q["last"] <= 0:
            continue
        mv = q["last"] - q["prev"]
        if abs(mv) >= Decimal("0.10") and d(m.get("volume_24h_fp")) >= minvol:
            out.append({
                "strategy": "movers", "key": f"movers:{m['ticker']}",
                "title": m.get("title"), "category": None,
                "detail": f"Last ${q['last']:.2f} vs prior ${q['prev']:.2f} ({'+' if mv > 0 else ''}{mv:.2f}).",
                "edge": "0", "actionable": False, "legs": [], "warning": "",
            })
    return out


def run_scan(client, cfg, db, series_cache: dict) -> dict:
    t0 = time.time()
    strategies = set(cfg.scanner["strategies"])
    cats = set(cfg.scanner.get("categories") or [])
    events = client.events(status="open", nested=True, max_items=cfg.scanner["max_markets_scanned"])
    if cats:
        events = [e for e in events if e.get("category") in cats]
    markets = [m for e in events for m in (e.get("markets") or [])]
    by_ticker = {m["ticker"]: m for m in markets}
    event_series = {e["event_ticker"]: e.get("series_ticker") for e in events}

    if not series_cache.get("ts") or time.time() - series_cache["ts"] > 6 * 3600:
        try:
            series_cache["map"] = {s["ticker"]: s for s in client.series_list()}
            series_cache["ts"] = time.time()
        except Exception:
            series_cache.setdefault("map", {})
    sf = series_cache.get("map", {})

    fvs = db.query("SELECT * FROM fair_values")
    missing = [f["ticker"] for f in fvs if f["ticker"] not in by_ticker]
    for i in range(0, len(missing), 100):
        for m in client.markets(status=None, tickers=missing[i:i + 100], exclude_combos=False):
            by_ticker[m["ticker"]] = m

    sigs = []
    if "basket_no" in strategies:
        sigs += basket_no(events, sf, cfg)
    if "basket_yes" in strategies:
        sigs += basket_yes(events, sf, cfg)
    if "fair_value" in strategies:
        sigs += fair_value(by_ticker, fvs, sf, event_series, cfg)
    if "wide_spread" in strategies:
        sigs += wide_spread(markets, cfg)
    if "movers" in strategies:
        sigs += movers(markets, cfg)
    sigs.sort(key=lambda s: (not s["actionable"], -float(s["edge"])))
    return {"signals": sigs, "events": len(events), "markets": len(markets), "secs": round(time.time() - t0, 1)}
