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
    se = math.sqrt(var * k / (k - 1)) / n if k > 1 else None
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
    return out


def board(wdb, bar: dict | None = None, mm_reports: list | None = None) -> dict:
    strategies = []
    for s in sources(wdb):
        sc = score(s.pop("rows"))
        strategies.append({**s, "kind": "pick", "score": sc, "verdict": judge(sc, bar)})
    for rp in mm_reports or []:                             # market-making / resting-order paper runs keep their own verdict
        if rp.get("status") != "running":
            continue
        v = rp.get("verdict") or {}
        qm = (rp.get("models") or {}).get("queue") or {}
        strategies.append({"key": f"mm_{rp.get('run_id')}", "name": f"Resting orders: run #{rp.get('run_id')}", "description": rp.get("name"),
                           "market": "Selected markets (market making)", "kind": "inventory", "open": None,
                           "score": {"trades": qm.get("fills", 0), "pnl": v.get("trading_pnl"), "days": rp.get("days")},
                           "verdict": {"status": "collecting" if not v.get("enough_data") else ("failing" if (v.get("trading_pnl") or 0) <= 0 else "review"),
                                       "progress_pct": None,
                                       "rules": [{"rule": "own test", "ok": bool(v.get("enough_data")), "detail": f"{v.get('needs')}; now {v.get('status')}"},
                                                 {"rule": "auto-live", "ok": False, "detail": "holds inventory: needs a different live engine, not eligible for automatic promotion"}]}})
    order = {"qualified": 0, "review": 1, "collecting": 2, "failing": 3}
    strategies.sort(key=lambda s: (order.get(s["verdict"]["status"], 9), -(s["score"].get("trades") or 0)))
    return {"bar": {**DEFAULT_BAR, **(bar or {})}, "strategies": strategies,
            "qualified": [s["key"] for s in strategies if s["verdict"]["status"] == "qualified"]}
