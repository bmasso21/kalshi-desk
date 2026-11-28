"""Player props, paper test: resting bids at the back of the queue, BEFORE the game only.

The trade study on Kalshi's NBA and NFL player-prop markets (13,143 markets, Jul-Nov 2026, run 25 Nov 2026) found the
same pattern as Mentions: the side that crosses the spread LOSES from 1 to 24 hours before the close (resting side
+3.3c at 1-6 h, t 2.6; +5.9c at 6-24 h, t 3.5) and WINS in the last hour (takers +6.0c, t 7.2 - lineup news).

Version 1 (25-28 Nov 2026) pulled the bids one hour before Kalshi's expected expiration. For props that time is the
EXPECTED END of the game (about tip-off + 3 h), so the bids sat still through most of the game and were sold into
whenever a player clearly hit or missed his number: 1,753 filled orders, -$2,343 on $7,203 (-33%). Those orders stay
in table prop_orders for the record.

Version 2 (this one, table prop_orders2): the real start time of each game comes from ESPN's scoreboard (matched to
Kalshi's game event, whose ticker ends the same way as the props'); a bid is placed only on a game starting 30 minutes
to 24 hours from now and is pulled AT the start. Props whose game cannot be matched are left alone. Filled only by
real trades after the queue ahead of it; held to settlement. Read-only public data. Places no orders."""
from __future__ import annotations

import datetime as dt
import time

import requests

from .finalwatch import ESPN, LEAGUES, _ts
from .mentions import MentionsPaper
from .sports import match_fixture

PROP_SERIES = ("KXNBAPTSPROT", "KXNBAREBPROT", "KXNBA3PTPROT", "KXNFLPASSTDS", "KXNFLTD")
PROP_LEAGUES = ("nba", "nfl")
MIN_LEAD_S = 1800                    # place only on games starting at least 30 min from now
MAX_LEAD_S = 24 * 3600
STARTS_EVERY = 1800                  # re-read the schedules every 30 minutes


def schedule(js: dict) -> list[dict]:
    """ESPN scoreboard -> games not started yet: start, teams (name, abbr)."""
    out = []
    for e in (js or {}).get("events") or []:
        st = ((e.get("status") or {}).get("type") or {})
        cs = ((e.get("competitions") or [{}])[0]).get("competitors") or []
        if st.get("state") != "pre" or len(cs) != 2 or not _ts(e.get("date")):
            continue
        out.append({"start": _ts(e.get("date")), "teams": [{"name": (c.get("team") or {}).get("displayName") or "",
                                                            "abbr": (c.get("team") or {}).get("abbreviation") or ""} for c in cs]})
    return out


class PropsMaker(MentionsPaper):
    TABLE = "prop_orders2"
    CATEGORY = "player-prop"

    def __init__(self, cfg_getter, wdb, kalshi_client, session=None):
        super().__init__(cfg_getter, wdb, kalshi_client)
        self.http = session or requests.Session()
        self._starts: dict = {}          # Kalshi event suffix (e.g. 26NOV28ORLWAS) -> ESPN start
        self._starts_ts = 0.0

    def series(self, now: float) -> list[str]:
        ss = list(self.opt("prop_series", PROP_SERIES))
        self.state["series"] = len(ss)
        return ss

    def refresh_starts(self, now: float):
        starts, games, err = {}, 0, []
        et = (dt.datetime.fromtimestamp(now, dt.timezone.utc) - dt.timedelta(hours=5)).date()
        for lg in PROP_LEAGUES:
            path, extra, series = LEAGUES[lg]
            try:
                evs = {}
                for e in self.k.paginate("/events", "events", {"series_ticker": series, "status": "open", "with_nested_markets": "true", "limit": 200}, max_items=400):
                    evs[e.get("event_ticker")] = e.get("markets") or []
                for d in (et, et + dt.timedelta(days=1)):
                    r = self.http.get(ESPN.format(path=path), params={**extra, "dates": d.strftime("%Y%m%d")}, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
                    r.raise_for_status()
                    for g in schedule(r.json()):
                        games += 1
                        a, b = g["teams"]
                        mt = match_fixture({"start_ts": g["start"], "p1": a["name"], "p2": b["name"], "p1_abbr": a["abbr"], "p2_abbr": b["abbr"]}, evs)
                        if mt and "-" in (mt[0] or ""):
                            starts[mt[0].split("-", 1)[1]] = g["start"]
            except Exception as e:
                err.append(f"{lg}: {str(e)[:120]}")
        if starts or not err:
            self._starts = starts
        self._starts_ts = now
        self.state.update(games_scheduled=games, games_matched=len(starts), schedule_error="; ".join(err) or None)

    def stop_time(self, m: dict, event: str, end: float, now: float) -> float | None:
        """Pulled at the game's start (ESPN); only for games starting 30 min to 24 h from now."""
        if now - self._starts_ts > STARTS_EVERY:
            self.refresh_starts(now)
        st = self._starts.get(event.split("-", 1)[1] if "-" in event else "")
        if st is None or not now + MIN_LEAD_S <= st <= now + MAX_LEAD_S:
            return None
        return st

    def status(self) -> dict:
        out = super().status()
        out["window_h"] = [MIN_LEAD_S / 3600, MAX_LEAD_S / 3600]
        out["pulled_at"] = "game start (ESPN)"
        try:
            v1 = self.db.query("SELECT COUNT(*) n, COALESCE(SUM(pnl), 0) pnl, COALESCE(SUM(CASE WHEN status='filled' THEN filled * price END), 0) staked "
                               "FROM prop_orders WHERE status='filled'")[0]
            out["v1"] = {"filled": v1["n"], "pnl": round(v1["pnl"], 2), "staked": round(v1["staked"], 2)}
        except Exception:
            out["v1"] = None
        return out
