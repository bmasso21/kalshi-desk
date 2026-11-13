"""Read-only history of the PRODUCTION account, available while the desk itself stays on demo.

The only thing this module can do with the production key is sign GET requests to two endpoints: the account's own
fills and settlements. ReadOnlyClient refuses everything else before a request is built, so nothing here can place,
change or cancel an order, whatever calls it. Nothing is stored; the result is computed and returned.
"""
from __future__ import annotations

import datetime as dt
import time

from .config import BASE_URLS
from .kalshi_client import KalshiClient

ALLOWED = ("/portfolio/fills", "/portfolio/settlements")


class ReadOnlyClient(KalshiClient):
    def request(self, method: str, path: str, params=None, body=None, auth: bool = False, retries: int = 5) -> dict:
        if method.upper() != "GET" or path.split("?")[0] not in ALLOWED or body is not None:
            raise PermissionError(f"read-only production history: {method} {path} is not allowed")
        return super().request("GET", path, params=params, body=None, auth=True, retries=retries)


def _ts(v) -> float:
    if v is None:
        return 0.0
    try:
        return float(v) / (1000 if float(v) > 1e11 else 1)
    except (TypeError, ValueError):
        try:
            return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0


def _num(*vals, cents=False):
    for v in vals:
        if v in (None, ""):
            continue
        try:
            return float(v) / (100 if cents else 1)
        except (TypeError, ValueError):
            continue
    return None


def norm_fill(f: dict) -> dict | None:
    """One fill as a signed YES quantity at a YES price. Buying NO at 30c is the same as selling YES at 70c."""
    q = _num(f.get("count_fp"), f.get("count"))
    p = _num(f.get("yes_price_dollars"), f.get("yes_price_fixed"))
    if p is None:
        p = _num(f.get("yes_price"), cents=True)
    if p is None and _num(f.get("no_price_dollars")) is not None:
        p = 1 - _num(f.get("no_price_dollars"))
    if not q or p is None:
        return None
    bs = f.get("book_side")
    if bs in ("bid", "ask"):
        sign = 1 if bs == "bid" else -1
    else:
        act, side = (f.get("action") or "buy").lower(), (f.get("side") or f.get("outcome_side") or f.get("purchased_side") or "").lower()
        if side not in ("yes", "no"):
            return None
        sign = (1 if side == "yes" else -1) * (1 if act == "buy" else -1)
    fee = _num(f.get("fee_cost"), f.get("fee_cost_dollars")) or 0.0
    return {"ticker": f.get("ticker") or f.get("market_ticker"), "q": sign * q, "p": p, "fee": fee,
            "taker": bool(f.get("is_taker")), "ts": _ts(f.get("ts_ms") or f.get("ts") or f.get("created_time"))}


def series_of(ticker: str) -> str:
    return (ticker or "").split("-")[0]


def summarize(fills: list[dict], settlements: list[dict], prefix: str = "") -> dict:
    res = {s.get("ticker"): (s.get("market_result") or "").lower() for s in settlements}
    mk: dict[str, dict] = {}
    skipped = 0
    for raw in sorted(fills, key=lambda f: _ts(f.get("ts_ms") or f.get("ts") or f.get("created_time"))):
        f = norm_fill(raw)
        if not f or not f["ticker"]:
            skipped += 1
            continue
        if prefix and not f["ticker"].upper().startswith(prefix.upper()):
            continue
        m = mk.setdefault(f["ticker"], {"ticker": f["ticker"], "pos": 0.0, "cash": 0.0, "fees": 0.0, "fills": 0, "taker_fills": 0,
                                        "first": f["ts"], "last": f["ts"], "peak_cost": 0.0, "opened": 0.0, "closed_by_sale": 0.0,
                                        "entry_side": None, "entry_px_sum": 0.0, "entry_qty": 0.0})
        before = m["pos"]
        m["pos"] = round(before + f["q"], 6)
        m["cash"] -= f["q"] * f["p"]
        m["fees"] += f["fee"]
        m["fills"] += 1
        m["taker_fills"] += 1 if f["taker"] else 0
        m["last"] = f["ts"]
        if before == 0 or (before > 0) == (f["q"] > 0):               # opening or adding
            side = "yes" if f["q"] > 0 else "no"
            m["entry_side"] = m["entry_side"] or side
            px = f["p"] if f["q"] > 0 else 1 - f["p"]                  # what was paid per contract for the side held
            m["entry_px_sum"] += abs(f["q"]) * px
            m["entry_qty"] += abs(f["q"])
            m["opened"] += abs(f["q"])
        else:                                                         # reducing: sold before settlement
            m["closed_by_sale"] += min(abs(f["q"]), abs(before))
        cost = abs(m["pos"]) * (m["entry_px_sum"] / m["entry_qty"] if m["entry_qty"] else 0)
        m["peak_cost"] = max(m["peak_cost"], cost)
    rows = []
    for t, m in mk.items():
        r = res.get(t)
        if abs(m["pos"]) > 1e-9 and r not in ("yes", "no"):
            status, pnl = "open", None
        else:
            settle = m["pos"] * (1.0 if r == "yes" else 0.0) if abs(m["pos"]) > 1e-9 else 0.0
            pnl = m["cash"] + settle - m["fees"]
            if abs(m["pos"]) < 1e-9:
                status = "sold before the end"
            elif m["closed_by_sale"] > 0:
                status = "partly sold, rest held to the end"
            else:
                status = "held to the end"
        entry = m["entry_px_sum"] / m["entry_qty"] if m["entry_qty"] else None
        rows.append({"ticker": t, "series": series_of(t), "status": status, "side": m["entry_side"], "contracts": round(m["opened"], 2),
                     "entry_c": round(100 * entry, 1) if entry is not None else None, "staked": round(m["entry_px_sum"], 2),
                     "fees": round(m["fees"], 4), "pnl": round(pnl, 4) if pnl is not None else None, "result": r or None,
                     "fills": m["fills"], "taker_fills": m["taker_fills"], "first": m["first"], "last": m["last"],
                     "held_sec": round(m["last"] - m["first"]) if status == "sold before the end" else None})

    def agg(rs):
        done = [x for x in rs if x["pnl"] is not None]
        wins = [x for x in done if x["pnl"] > 0]
        losses = [x for x in done if x["pnl"] < 0]
        staked = sum(x["staked"] for x in done)
        net = sum(x["pnl"] for x in done)
        fees = sum(x["fees"] for x in done)
        return {"markets": len(done), "open": len(rs) - len(done), "wins": len(wins), "losses": len(losses),
                "flat": len(done) - len(wins) - len(losses),
                "win_pct": round(100 * len(wins) / len(done), 1) if done else None,
                "avg_win": round(sum(x["pnl"] for x in wins) / len(wins), 4) if wins else None,
                "avg_loss": round(sum(x["pnl"] for x in losses) / len(losses), 4) if losses else None,
                "biggest_loss": round(min((x["pnl"] for x in losses), default=0), 4) if losses else None,
                "staked": round(staked, 2), "fees": round(fees, 4), "net": round(net, 4),
                "before_fees": round(net + fees, 4), "return_pct": round(100 * net / staked, 2) if staked else None,
                "breakeven_win_pct": (round(100 * -(sum(x["pnl"] for x in losses) / len(losses)) /
                                            ((sum(x["pnl"] for x in wins) / len(wins)) - (sum(x["pnl"] for x in losses) / len(losses))), 1)
                                      if wins and losses else None),
                "taker_fill_pct": round(100 * sum(x["taker_fills"] for x in rs) / max(1, sum(x["fills"] for x in rs)), 1)}

    by_series, by_status = {}, {}
    for x in rows:
        by_series.setdefault(x["series"], []).append(x)
        by_status.setdefault(x["status"], []).append(x)
    rows.sort(key=lambda x: -x["last"])
    return {"all": agg(rows),
            "by_series": sorted(({"series": k, **agg(v)} for k, v in by_series.items()), key=lambda x: -x["markets"]),
            "by_exit": [{"exit": k, **agg(v)} for k, v in by_status.items() if k != "open"],
            "markets": rows[:200], "fills_read": len(fills), "fills_unreadable": skipped, "settlements_read": len(settlements)}


def prod_history(cfg, days: float = 30, prefix: str = "", max_items: int = 5000) -> dict:
    kid, kp = cfg.data.get("prod_key_id"), cfg.data.get("prod_private_key_path")
    if not kid or not kp:
        return {"ok": False, "error": "No production API key is set up in Settings > Connection."}
    c = ReadOnlyClient(cfg.data.get("base_url_override") or BASE_URLS["prod"], kid, kp)
    since = int(time.time() - max(1, min(365, days)) * 86400)
    fills = list(c.paginate("/portfolio/fills", "fills", {"min_ts": since, "limit": 200}, max_items=max_items))
    setts = list(c.paginate("/portfolio/settlements", "settlements", {"min_ts": since, "limit": 200}, max_items=max_items))
    out = summarize(fills, setts, prefix)
    out.update(ok=True, days=days, prefix=prefix, truncated=len(fills) >= max_items, read_at=time.time())
    return out
