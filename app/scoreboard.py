"""One scoreboard for every paper strategy, judged against one qualifying bar.

Each strategy's settled paper trades are scored the same way: trades, distinct events (games / hourly closes), win
rate, P&L after fees, return on stake, a t-statistic that treats trades settling together as one (so 20 strikes of
the same hour don't count as 20 independent results), and the first and second half of its history separately.

A strategy QUALIFIES only when every rule of the bar holds. Qualifying does not trade anything by itself: the live
executor that would act on it (inside the cap the owner sets) is a separate, later piece."""
from __future__ import annotations

import math

DEFAULT_BAR = {"min_trades": 100, "min_events": 30, "min_t": 2.0}

AUTOLIVE_DEFAULT = {"enabled": False, "total_cap": 0.0, "per_strategy_cap": 0.0, "per_trade_max": 0.0, "daily_loss_stop": 0.0}


MIN_EVENTS_FOR_T = 5


def score(rows: list[dict]) -> dict:
    """rows: [{pnl, cost, cluster, ts}] settled paper trades of ONE strategy."""
    if not rows:
        return {"trades": 0, "events": 0}
    rows = sorted(rows, key=lambda r: (r["cluster"], r["ts"]))
    n = len(rows)
    pnl = sum(r["pnl"] for r in rows)
    cost = sum(r["cost"] for r in rows)
    mean = pnl / n
    cl = {}
    for r in rows:
        c = cl.setdefault(r["cluster"], [0.0, 0])
        c[0] += r["pnl"]
        c[1] += 1
    k = len(cl)
    var = sum((s - mean * m) ** 2 for s, m in cl.values())
    # t needs enough separate events to mean anything: two losing trades of almost the same size gave "t -41"
    se = math.sqrt(var * k / (k - 1)) / n if k >= MIN_EVENTS_FOR_T else None
    by_time = sorted(rows, key=lambda r: r["ts"])
    mid = by_time[len(by_time) // 2]["ts"]

    def half(rs):
        if not rs:
            return {"trades": 0, "pnl": 0.0, "return_pct": None}
        p, c = sum(r["pnl"] for r in rs), sum(r["cost"] for r in rs)
        return {"trades": len(rs), "pnl": round(p, 2), "return_pct": round(100 * p / c, 2) if c else None}
    return {"trades": n, "events": k, "wins": sum(1 for r in rows if r["pnl"] > 0), "pnl": round(pnl, 2), "staked": round(cost, 2),
            "return_pct": round(100 * pnl / cost, 2) if cost else None, "per_trade_c": round(100 * mean, 2),
            "t": round(mean / se, 2) if se else None,
            "first_half": half([r for r in rows if r["ts"] < mid]), "second_half": half([r for r in rows if r["ts"] >= mid]),
            "first_ts": by_time[0]["ts"], "last_ts": by_time[-1]["ts"]}


def judge(s: dict, bar: dict) -> dict:
    """The bar, rule by rule, with how far along the strategy is."""
    b = {**DEFAULT_BAR, **(bar or {})}
    rules = [("trades", s.get("trades", 0) >= b["min_trades"], f"{s.get('trades', 0)}/{b['min_trades']} settled trades"),
             ("events", s.get("events", 0) >= b["min_events"], f"{s.get('events', 0)}/{b['min_events']} separate games/hours"),
             ("profit", (s.get("pnl") or 0) > 0, f"P&L after fees ${s.get('pnl', 0):+.2f}"),
             ("both_halves", bool(s.get("first_half", {}).get("pnl", 0) > 0 and s.get("second_half", {}).get("pnl", 0) > 0),
              f"first half ${s.get('first_half', {}).get('pnl', 0):+.2f}, second ${s.get('second_half', {}).get('pnl', 0):+.2f}"),
             ("significant", (s.get("t") or 0) >= b["min_t"], f"t = {s.get('t')} (needs {b['min_t']})")]
    ok = all(r[1] for r in rules)
    progress = min(1.0, s.get("trades", 0) / b["min_trades"]) if b["min_trades"] else 1.0
    status = "qualified" if ok else ("collecting" if s.get("trades", 0) < b["min_trades"] or s.get("events", 0) < b["min_events"] else "failing")
    return {"status": status, "progress_pct": round(100 * progress), "rules": [{"rule": r[0], "ok": r[1], "detail": r[2]} for r in rules]}


# ------------------------------------------------------------------ sources --
def sources(wdb) -> list[dict]:
    """Every paper strategy the app runs, as (name, description, settled rows, open count)."""
    out = []

    def q(sql, args=()):
        try:
            return wdb.query(sql, args)
        except Exception:
            return []                                       # a table that does not exist yet = no trades yet
    fv = q("SELECT tier, sport, price, fee, pnl, commence_ts, ts, status FROM fv_picks2")
    for tier, label, desc in (("edge", "Sports: 2c+ under sportsbook fair", "Kalshi priced 2c+ below the de-vigged sportsbook consensus, bought before the game"),
                              ("lean", "Sports: at/under sportsbook fair", "Kalshi at or below the sportsbook consensus (favourites/near-even, 30c+)")):
        rs = [r for r in fv if r["tier"] == tier]
        out.append({"key": f"sports_{tier}", "name": label, "description": desc, "market": "Sports (NFL, college football, NBA, NHL)",
                    "rows": [{"pnl": r["pnl"], "cost": r["price"] + (r["fee"] or 0), "cluster": r["commence_ts"], "ts": r["commence_ts"]}
                             for r in rs if r["status"] == "settled" and r["pnl"] is not None],
                    "open": sum(1 for r in rs if r["status"] == "open")})
    gw = q("SELECT ticker, side, check_min, gap, ask, fee, pnl, close_ts, status FROM gw_picks")
    first = {}
    for r in sorted(gw, key=lambda r: -r["check_min"]):
        if r["gap"] >= 15:
            first.setdefault((r["ticker"], r["side"]), r)
    out.append({"key": "gold_cushion15", "name": "Gold hourly: $15+ cushion", "description": "Favoured side when gold sits $15+ past the strike, at the real ask",
                "market": "Hourly gold (KXGOLDH)",
                "rows": [{"pnl": r["pnl"], "cost": r["ask"] + r["fee"], "cluster": r["close_ts"], "ts": r["close_ts"]} for r in first.values() if r["status"] == "settled"],
                "open": sum(1 for r in first.values() if r["status"] == "open")})
    gf = q("SELECT ticker, side, check_min, ask, fee, pnl, close_ts, status FROM gw_fv_picks")
    ff = {}
    for r in sorted(gf, key=lambda r: -r["check_min"]):
        ff.setdefault((r["ticker"], r["side"]), r)
    out.append({"key": "gold_fv", "name": "Gold hourly: 2c+ under model fair", "description": "Live gold model, 30-60 min before the close, 20-80c, at the real ask",
                "market": "Hourly gold (KXGOLDH)",
                "rows": [{"pnl": r["pnl"], "cost": r["ask"] + r["fee"], "cluster": r["close_ts"], "ts": r["close_ts"]} for r in ff.values() if r["status"] == "settled"],
                "open": sum(1 for r in ff.values() if r["status"] == "open")})
    fd = q("SELECT event, price, fee, pnl, settled_ts, status FROM fed_picks")
    out.append({"key": "fed", "name": "Fed decisions: 3c+ under futures-implied odds", "description": "Kalshi's FOMC outcome odds vs 30-day fed funds futures (FedWatch method)",
                "market": "Fed decisions (KXFEDDECISION)",
                "rows": [{"pnl": r["pnl"], "cost": r["price"] + r["fee"], "cluster": r["event"], "ts": r["settled_ts"]} for r in fd if r["status"] == "settled" and r["pnl"] is not None],
                "open": sum(1 for r in fd if r["status"] == "open")})
    gp = q("SELECT side, day, close_ts, price, fee, pnl, status FROM gas_picks")
    for sd, label in (("no", "Gas daily: NO 5c+ under trend model"), ("yes", "Gas daily: YES 5c+ under trend model")):
        rs = [r for r in gp if r["side"] == sd]
        out.append({"key": f"gas_{sd}", "name": label, "market": "Daily gas price (KXAAAGASD)",
                    "description": "Tomorrow's AAA figure from the last 3 days' trend; bought 8am-6pm ET at the real ask, 20-95c. "
                                   "The backtest paid on NO only, possibly just a falling season: the two sides are scored apart",
                    "rows": [{"pnl": r["pnl"], "cost": r["price"] + r["fee"], "cluster": r["day"], "ts": r["close_ts"]} for r in rs if r["status"] == "settled" and r["pnl"] is not None],
                    "open": sum(1 for r in rs if r["status"] == "open")})
    pall = q("SELECT event, sport, price, fee, pnl, start_ts, status FROM poly_picks")
    for key, name, market, desc, pp in (
            ("poly_gap", "Games: Kalshi 3c+ under Polymarket", "NBA, NFL, NHL game winners",
             "Kalshi's ask 3c or more below Polymarket's midpoint after the fee (Polymarket spread 3c or less), bought before the start; checked every 5 minutes",
             [r for r in pall if r["sport"] != "fed"]),
            ("poly_fed", "Fed decisions: Kalshi 3c+ under Polymarket", "Fed decisions (KXFEDDECISION)",
             "Each outcome of a Fed meeting: Kalshi's ask 3c or more below Polymarket's midpoint after the fee; settles after the meeting",
             [r for r in pall if r["sport"] == "fed"])):
        out.append({"key": key, "name": name, "market": market, "description": desc,
                    "rows": [{"pnl": r["pnl"], "cost": r["price"] + r["fee"], "cluster": r["event"], "ts": r["start_ts"]} for r in pp if r["status"] == "settled" and r["pnl"] is not None],
                    "open": sum(1 for r in pp if r["status"] == "open")})
    fp = q("SELECT event, price, size, fee, pnl, settled_ts, ts, status FROM final_picks")
    out.append({"key": "sports_final", "name": "Games already decided: buy the winner after the final", "market": "NBA, NFL, NHL, college game winners",
                "description": "When ESPN shows a game final and Kalshi still offers the winner at 99c or less, 100 contracts are bought on paper "
                               "(fee rounded up to the cent on the order). Scored per contract",
                "rows": [{"pnl": r["pnl"] / r["size"], "cost": r["price"] + r["fee"] / r["size"], "cluster": r["event"], "ts": r["settled_ts"] or r["ts"]}
                         for r in fp if r["status"] == "settled" and r["pnl"] is not None and r["size"]],
                "open": sum(1 for r in fp if r["status"] == "open")})
    fl = q("SELECT event, price, fee, pnl, close_ts, status FROM flow_picks")
    out.append({"key": "flow_weather", "name": "Weather: follow large first-hour trades", "market": "New weather markets (first hour)",
                "description": "A single trade of 100+ contracts in a new weather market's first hour; the same side is bought at the real ask within 10 minutes "
                               "if it costs no more than 1c above that trade. Backtest: +1.85c a contract, t 2.0 - suggestive, not proven",
                "rows": [{"pnl": r["pnl"], "cost": r["price"] + r["fee"], "cluster": r["event"], "ts": r["close_ts"]} for r in fl if r["status"] == "settled" and r["pnl"] is not None],
                "open": sum(1 for r in fl if r["status"] == "open")})
    mo = q("SELECT event, price, filled, pnl, close_ts, status FROM men_orders")
    out.append({"key": "mentions_maker", "name": "Mentions: resting bids, back of the queue", "market": "Mentions (will a speaker say a word)",
                "description": "One resting bid per side at the best bid, 1-24 h before the close, filled only by real trades after the queue ahead; held to settlement. "
                               "Each trade is one filled order (up to 10 contracts)",
                "rows": [{"pnl": r["pnl"], "cost": r["filled"] * r["price"], "cluster": r["event"], "ts": r["close_ts"]} for r in mo if r["status"] == "filled" and r["pnl"] is not None],
                "open": sum(1 for r in mo if r["status"] == "resting")})
    ar = q("SELECT kind, event, cost, pnl_real, settled_ts, status, locked FROM arb_opps")
    for kinds, key, label, desc in ((("no_basket", "ladder"), "arb_locked", "Arbitrage: locked (NO baskets, ladders)",
                                     "Kalshi priced against itself: every outcome pays at least the cost after fees, confirmed at live order-book prices"),
                                    (("yes_basket",), "arb_yes", "Arbitrage: YES baskets (only if one outcome must win)",
                                     "All outcomes' YES under $1 after fees; safe only when the event is exhaustive")):
        rs = [r for r in ar if r["kind"] in kinds]
        out.append({"key": key, "name": label, "description": desc, "market": "Every open Kalshi event",
                    "rows": [{"pnl": r["pnl_real"], "cost": r["cost"], "cluster": r["event"], "ts": r["settled_ts"]} for r in rs if r["status"] == "settled" and r["pnl_real"] is not None],
                    "open": sum(1 for r in rs if r["status"] in ("open", "gone"))})
    return out


RETIRED = {"gold_fv": "retired 18 Nov: -25% on 51 paper buys (the free live gold price is too noisy)",
           "sports_lean": "retired 24 Nov: 41 settled, 13 won, -$5.69 (-30% on stake), t -2.07 over 29 games",
           "gold_cushion15": "retired 19 Nov: 339 of 353 won yet lost $5.14 (the rare miss costs more than the many small wins pay)"}


def board(wdb, bar: dict | None = None, mm_reports: list | None = None) -> dict:
    strategies = []
    for s in sources(wdb):
        sc = score(s.pop("rows"))
        v = judge(sc, bar)
        if s["key"] in RETIRED:
            v = {"status": "retired", "progress_pct": None, "rules": [{"rule": "retired", "ok": False, "detail": RETIRED[s["key"]]}]}
        strategies.append({**s, "kind": "pick", "score": sc, "verdict": v})
    for rp in mm_reports or []:                             # market-making / resting-order paper runs keep their own verdict
        if rp.get("status") != "running":
            continue                                        # stopped runs (e.g. #7, #10, #11 after failing) drop off the board
        v = rp.get("verdict") or {}
        qm = (rp.get("models") or {}).get("queue") or {}
        strategies.append({"key": f"mm_{rp.get('run_id')}", "name": f"Resting orders: run #{rp.get('run_id')}", "description": rp.get("name"),
                           "market": "Selected markets (market making)", "kind": "inventory", "open": None,
                           "score": {"trades": qm.get("fills", 0), "pnl": v.get("trading_pnl"), "days": rp.get("days")},
                           "verdict": {"status": "collecting" if not v.get("enough_data") else ("failing" if (v.get("trading_pnl") or 0) <= 0 else "review"),
                                       "progress_pct": None,
                                       "rules": [{"rule": "own test", "ok": bool(v.get("enough_data")), "detail": f"{v.get('needs')}; now {v.get('status')}"},
                                                 {"rule": "auto-live", "ok": False, "detail": "holds inventory: needs a different live engine, not eligible for automatic promotion"}]}})
    order = {"qualified": 0, "review": 1, "collecting": 2, "failing": 3, "retired": 4}
    strategies.sort(key=lambda s: (order.get(s["verdict"]["status"], 9), -(s["score"].get("trades") or 0)))
    return {"bar": {**DEFAULT_BAR, **(bar or {})}, "strategies": strategies,
            "qualified": [s["key"] for s in strategies if s["verdict"]["status"] == "qualified"]}
