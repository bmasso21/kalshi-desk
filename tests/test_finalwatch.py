"""Games already decided (paper): buying the winner after the final whistle. Hand-checkable."""
import datetime as dt
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import finalwatch as F  # noqa: E402
from app import scoreboard as SB  # noqa: E402
from app.warehouse import WarehouseDB  # noqa: E402

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def close(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


NOW = dt.datetime(2026, 11, 24, 3, 0, tzinfo=dt.timezone.utc).timestamp()        # 10 pm Eastern, Nov 23


def espn_game(gid, start, state, completed, a, b, sa, sb, win_a=None):
    def comp(t, s, w):
        c = {"team": {"displayName": t[0], "abbreviation": t[1]}, "score": str(s)}
        if w is not None:
            c["winner"] = w
        return c
    return {"id": gid, "shortName": f"{a[1]} @ {b[1]}", "date": start,
            "status": {"type": {"state": state, "completed": completed}},
            "competitions": [{"competitors": [comp(b, sb, None if win_a is None else not win_a), comp(a, sa, win_a)]}]}


IND, OKC = ("Indiana Pacers", "IND"), ("Oklahoma City Thunder", "OKC")
LAL, UTA = ("Los Angeles Lakers", "LAL"), ("Utah Jazz", "UTAH")
SAS, SAC = ("San Antonio Spurs", "SA"), ("Sacramento Kings", "SAC")


class Resp:
    def __init__(self, j):
        self.j = j

    def raise_for_status(self):
        pass

    def json(self):
        return self.j


class Ses:
    def __init__(self):
        self.board = {"events": []}
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        assert "site.api.espn.com" in url
        return Resp(self.board if "basketball/nba" in url else {"events": []})


def km(t, sub, bid, ask, bsz=500, asz=500, status="active"):
    return {"ticker": t, "yes_sub_title": sub, "yes_bid_dollars": str(bid), "yes_ask_dollars": str(ask), "yes_bid_size_fp": str(bsz),
            "yes_ask_size_fp": str(asz), "status": status, "close_time": "2026-11-27T02:00:00Z"}


class K:
    def __init__(self):
        self.m = {"KXNBAGAME-26NOV23OKCIND-OKC": km("KXNBAGAME-26NOV23OKCIND-OKC", "Oklahoma City", 0.00, 0.01),
                  "KXNBAGAME-26NOV23OKCIND-IND": km("KXNBAGAME-26NOV23OKCIND-IND", "Indiana", 0.98, 0.99, asz=2000),
                  "KXNBAGAME-26NOV23UTALAL-UTA": km("KXNBAGAME-26NOV23UTALAL-UTA", "Utah", 0.02, 0.04),
                  "KXNBAGAME-26NOV23UTALAL-LAL": km("KXNBAGAME-26NOV23UTALAL-LAL", "Los Angeles L", 0.97, 0.995, asz=30),
                  "KXNBAGAME-26NOV23SASSAC-SAS": km("KXNBAGAME-26NOV23SASSAC-SAS", "San Antonio", 0.99, 1.0, status="closed"),
                  "KXNBAGAME-26NOV23SASSAC-SAC": km("KXNBAGAME-26NOV23SASSAC-SAC", "Sacramento", 0.0, 0.01, status="closed")}
        self.res = {}
        self.calls = []

    def paginate(self, path, key, params, max_items=None):
        self.calls.append((path, params.get("series_ticker")))
        assert path == "/events" and params["series_ticker"] == "KXNBAGAME"
        evs = {}
        for t, m in self.m.items():
            evs.setdefault(t.rsplit("-", 1)[0], []).append(m)
        for e, ms in evs.items():
            yield {"event_ticker": e, "markets": ms}

    def market(self, t):
        self.calls.append(("market", t))
        return dict(self.m[t])

    def markets(self, status=None, tickers=None, event_ticker=None, max_items=None, **kw):
        if tickers:
            return [{"ticker": t, "result": self.res.get(t, "")} for t in tickers]
        return [m for t, m in self.m.items() if t.startswith(event_ticker + "-")]


def main():
    check("Kalshi's fee is rounded up to the cent on the whole order: 100 at 99c = 7c (6.93c exact); 1 at 99c = 1c",
          close(F.order_fee(0.99, 100), 0.07) and close(F.order_fee(0.99, 1), 0.01) and close(F.order_fee(0.50, 100), 1.75))
    s, k = Ses(), K()
    wdb = WarehouseDB(Path(tempfile.mkdtemp()) / "w.db")
    fw = F.FinalWatch(lambda: {"final_enabled": True}, wdb, k, session=s)
    start = "2026-11-24T00:00Z"
    s.board = {"events": [espn_game("1", start, "in", False, IND, OKC, 100, 98),                       # still playing: nothing
                          espn_game("2", start, "post", True, LAL, UTA, 99, 99, True)]}               # tied score: never guess
    r = fw.scan(NOW)
    check("a game still on, or a final without a clear winner, is ignored; the league is checked again in a minute",
          r == {"new_finals": 0, "picks": 0} and fw.state["by_league"]["nba"]["active"] is True and close(fw._next["nba"], NOW + 60), (r, fw.state))
    check("idle leagues are checked every 15 minutes", close(fw._next["nfl"], NOW + 900) and close(fw._next["ncaab"], NOW + 900))
    check("college basketball is read for all of Division I (groups=50), not only the featured games",
          any("mens-college-basketball" in u and p.get("groups") == "50" for u, p in s.calls))

    s.board = {"events": [espn_game("1", start, "post", True, IND, OKC, 112, 104, True),             # IND won: 99c ask with 2,000 offered
                          espn_game("3", start, "post", True, LAL, UTA, 120, 101, True),             # LAL won: 99.5c ask, and only 30
                          espn_game("4", start, "post", True, SAS, SAC, 130, 110, True)]}            # Kalshi already closed
    k.m["KXNBAGAME-26NOV23UTALAL-UTA"]["yes_bid_dollars"] = "0.0"                                   # nothing cheaper on the loser side either
    r = fw.scan(NOW + 60)
    g = {x["game_id"]: x for x in wdb.query("SELECT * FROM final_games")}
    pk = wdb.query("SELECT * FROM final_picks")
    check("final: the Kalshi game is found by date and teams; the winner is bought at its 99c ask, 100 contracts, fee 7c",
          len(pk) == 1 and pk[0]["ticker"] == "KXNBAGAME-26NOV23OKCIND-IND" and pk[0]["side"] == "yes" and close(pk[0]["price"], .99)
          and pk[0]["size"] == 100 and close(pk[0]["fee"], .07) and g["1"]["status"] == "bought", (pk, g.get("1")))
    check("a winner priced above 99c (with nothing cheaper on the loser) is not bought; a closed Kalshi market is recorded as already closed",
          g["3"]["status"] == "nothing_under_99c" and g["4"]["status"] == "already_closed" and r["new_finals"] == 3, g)
    n = len(k.calls)
    fw._next = {}
    fw.scan(NOW + 120)
    check("each final is handled once", len(wdb.query("SELECT * FROM final_games")) == 3 and len(wdb.query("SELECT * FROM final_picks")) == 1
          and not any(c[0] == "market" for c in k.calls[n:]))

    # the loser's NO can be the cheaper way in
    s2, k2 = Ses(), K()
    k2.m["KXNBAGAME-26NOV23OKCIND-IND"]["yes_ask_dollars"] = "0.99"
    k2.m["KXNBAGAME-26NOV23OKCIND-OKC"]["yes_bid_dollars"] = "0.02"                                 # NO on OKC costs 98c
    k2.m["KXNBAGAME-26NOV23OKCIND-OKC"]["yes_bid_size_fp"] = "300"
    s2.board = {"events": [espn_game("1", start, "post", True, IND, OKC, 112, 104, True)]}
    db2 = WarehouseDB(Path(tempfile.mkdtemp()) / "b.db")
    F.FinalWatch(lambda: {"final_enabled": True}, db2, k2, session=s2).scan(NOW)
    p2 = db2.query("SELECT * FROM final_picks")[0]
    check("the cheaper of the two equal bets is taken: NO on the loser at 98c instead of the winner at 99c",
          p2["ticker"] == "KXNBAGAME-26NOV23OKCIND-OKC" and p2["side"] == "no" and close(p2["price"], .98) and close(p2["fee"], F.order_fee(.98, 100)), p2)
    k3 = K()
    k3.m["KXNBAGAME-26NOV23OKCIND-IND"]["yes_ask_size_fp"] = "40"
    db3 = WarehouseDB(Path(tempfile.mkdtemp()) / "c.db")
    F.FinalWatch(lambda: {"final_enabled": True}, db3, k3, session=s2).scan(NOW)
    check("fewer than 100 offered at the price -> recorded as too thin, nothing bought",
          db3.query("SELECT status FROM final_games")[0]["status"] == "too_thin" and not db3.query("SELECT 1 FROM final_picks"))

    # Kalshi closes the market: the window is measured; settlement
    k.m["KXNBAGAME-26NOV23OKCIND-IND"].update(status="closed", close_time=dt.datetime.fromtimestamp(NOW + 150, dt.timezone.utc).isoformat().replace("+00:00", "Z"))
    fw.track_close(NOW + 300)
    st = fw.status()
    check("how long Kalshi kept the market open after the final is recorded (90 s here)",
          st["close_after_final_s"]["n"] == 1 and st["close_after_final_s"]["median"] == 90, st["close_after_final_s"])
    k.res = {"KXNBAGAME-26NOV23OKCIND-IND": "yes"}
    fw.settle(NOW + 600)
    p = wdb.query("SELECT * FROM final_picks")[0]
    check("settles on Kalshi's result: 100 x (1 - 0.99) - 0.07 = +0.93", p["status"] == "settled" and close(p["pnl"], 0.93, 1e-6), p)
    row = {x["key"]: x for x in SB.board(wdb)["strategies"]}["sports_final"]
    check("the scoreboard scores it per contract (0.93c, cost 99.07c)", row["score"]["trades"] == 1 and close(row["score"]["per_trade_c"], 0.93, 0.01)
          and close(row["score"]["staked"], 0.99, 0.006), row["score"])
    check("the Kalshi client it is given has no order methods", not any(hasattr(k, a) for a in ("create_order", "cancel_order")))
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
