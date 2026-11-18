"""Everything open on Kalshi right now, summarised: every series with its category, frequency and settlement source, and
the open markets' 24-hour volume and open interest. Read-only public market data; runs in the background (about a
minute: one call for the series list, then one per 1,000 open markets)."""
from __future__ import annotations

import collections
import threading
import time


def fnum(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def summarize(series: list[dict], markets: list[dict]) -> dict:
    info = {s.get("ticker"): s for s in series}
    by = collections.defaultdict(lambda: {"open_markets": 0, "volume_24h": 0.0, "open_interest": 0.0, "events": set()})
    for m in markets:
        st = m.get("series_ticker") or (m.get("event_ticker") or "").split("-")[0]
        b = by[st]
        b["open_markets"] += 1
        b["volume_24h"] += fnum(m.get("volume_24h_fp") or m.get("volume_24h"))
        b["open_interest"] += fnum(m.get("open_interest_fp") or m.get("open_interest"))
        b["events"].add(m.get("event_ticker"))
    rows = []
    for st, b in by.items():
        s = info.get(st) or {}
        rows.append({"series": st, "title": s.get("title") or "", "category": s.get("category") or "(unlisted)",
                     "frequency": s.get("frequency") or "", "settles_on": ", ".join(x.get("name", "") for x in (s.get("settlement_sources") or []))[:120],
                     "fee_type": s.get("fee_type"), "open_markets": b["open_markets"], "open_events": len(b["events"]),
                     "volume_24h": round(b["volume_24h"]), "open_interest": round(b["open_interest"])})
    rows.sort(key=lambda r: -r["volume_24h"])
    cats = collections.defaultdict(lambda: {"series_listed": 0, "series_open": 0, "open_markets": 0, "volume_24h": 0, "open_interest": 0})
    for s in series:
        cats[s.get("category") or "(unlisted)"]["series_listed"] += 1
    for r in rows:
        c = cats[r["category"]]
        c["series_open"] += 1
        c["open_markets"] += r["open_markets"]
        c["volume_24h"] += r["volume_24h"]
        c["open_interest"] += r["open_interest"]
    freq = collections.Counter()
    for r in rows:
        freq[r["frequency"] or "?"] += r["volume_24h"]
    return {"series_listed": len(series), "series_open": len(rows), "open_markets": len(markets),
            "volume_24h": sum(r["volume_24h"] for r in rows),
            "categories": sorted(({"category": k, **v} for k, v in cats.items()), key=lambda x: -x["volume_24h"]),
            "volume_by_frequency": dict(freq.most_common()), "top_series": rows[:150]}


class Catalog:
    def __init__(self, wdb, kalshi_client):
        self.db = wdb
        self.k = kalshi_client            # public production market data only
        self.state = {"running": False, "error": None, "progress": ""}

    def run(self):
        self.state.update(running=True, error=None, progress="series list")
        try:
            series = list(self.k.paginate("/series", "series", {}, max_items=50000))
            markets = []
            for m in self.k.paginate("/markets", "markets", {"status": "open", "limit": 1000, "mve_filter": "exclude"}, max_items=400000):
                markets.append({k: m.get(k) for k in ("series_ticker", "event_ticker", "volume_24h_fp", "volume_24h", "open_interest_fp", "open_interest")})
                if len(markets) % 5000 == 0:
                    self.state["progress"] = f"{len(markets)} open markets read"
            out = summarize(series, markets)
            out["ts"] = time.time()
            self.db.put("catalog", out)
            self.state["progress"] = "done"
        except Exception as e:
            self.state["error"] = str(e)[:300]
        finally:
            self.state["running"] = False

    def start(self) -> bool:
        if self.state["running"]:
            return False
        threading.Thread(target=self.run, daemon=True, name="catalog").start()
        return True

    def latest(self) -> dict:
        return {"state": self.state, "catalog": self.db.get("catalog")}
