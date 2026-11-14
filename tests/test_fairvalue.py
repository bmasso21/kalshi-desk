"""Sports fair-value scanner: margin removal, matching, the pick rule, credit budget, closing line and settlement."""
import datetime as dt
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import fairvalue as FV  # noqa: E402
from app.warehouse import WarehouseDB  # noqa: E402

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def close(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._b, self.headers, self.text = status, body, headers or {}, str(body)

    def json(self):
        if self._b is None:
            raise ValueError
        return self._b


class Http:
    def __init__(self):
        self.calls, self.body, self.status, self.remaining = [], [], 200, 499

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        self.remaining -= 1
        return Resp(self.status, self.body, {"x-requests-remaining": str(self.remaining)})


class Kalshi:
    def __init__(self):
        self.mk = {}
        self.calls = []

    def markets(self, status="open", series_ticker=None, max_items=None, **kw):
        self.calls.append(("markets", series_ticker))
        return [m for m in self.mk.values() if m["ticker"].startswith(series_ticker + "-") and m.get("status") == "active"]

    def market(self, ticker):
        return {"market": self.mk[ticker]}

    def __getattr__(self, name):                       # anything else (orders!) must never be called
        raise AssertionError(f"fair value scanner called kalshi.{name}")


def book(key, home, hp, away, ap):
    return {"key": key, "markets": [{"key": "h2h", "outcomes": [{"name": home, "price": hp}, {"name": away, "price": ap}]}]}


def main():
    wdb = WarehouseDB(Path(tempfile.mkdtemp()) / "w.db")
    cfg = {"theodds_key": "k" * 32, "fv_monthly_cap": 3, "fv_sports": {"americanfootball_nfl": {"series": "KXNFLGAME", "markets": "h2h"}}}
    http, k = Http(), Kalshi()
    fv = FV.FairValue(lambda: cfg, wdb, k, session=http, base="http://odds.test/v4")
    # margin removal: 1.80 / 2.10 -> 0.5556 + 0.4762 = 1.0317 -> 53.85% / 46.15%
    p = FV.devig([{"name": "A", "price": 1.80}, {"name": "B", "price": 2.10}])
    check("bookmaker margin is removed (1.80 / 2.10 -> 53.85% / 46.15%)", close(p["A"], (1 / 1.8) / (1 / 1.8 + 1 / 2.1)) and close(p["A"] + p["B"], 1))
    check("three-way or broken prices are refused", FV.devig([{"name": "A", "price": 2}, {"name": "B", "price": 3}, {"name": "Draw", "price": 3}]) is None
          and FV.devig([{"name": "A", "price": 1.0}, {"name": "B", "price": 2}]) is None)
    start = time.time() + 6 * 3600
    d = dt.datetime.fromtimestamp(start - 5 * 3600, dt.timezone.utc)
    tag = d.strftime("%y%b%d").upper()
    iso = dt.datetime.fromtimestamp(start, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    H, A = "Green Bay Packers", "New Orleans Saints"
    ev = {"id": "e1", "sport_key": "americanfootball_nfl", "commence_time": iso, "home_team": H, "away_team": A,
          "bookmakers": [book("dk", H, 1.50, A, 2.70), book("fd", H, 1.52, A, 2.62), book("mgm", H, 1.48, A, 2.75), book("cz", H, 1.50, A, 2.65)]}
    c = FV.consensus(ev)
    hp = sorted((1 / a) / (1 / a + 1 / b) for a, b in ((1.50, 2.70), (1.52, 2.62), (1.48, 2.75), (1.50, 2.65)))
    check("fair price = median across books; book count and disagreement reported", close(c["p_home"], (hp[1] + hp[2]) / 2) and c["books"] == 4 and close(c["range"], hp[3] - hp[0]), c)
    fair = c["p_home"]                                               # about 0.640
    EV = f"KXNFLGAME-{tag}NOGB"
    k.mk = {f"{EV}-GB": {"ticker": f"{EV}-GB", "event_ticker": EV, "status": "active", "yes_sub_title": "Green Bay", "yes_bid_dollars": "0.5700", "yes_ask_dollars": "0.5900"},
            f"{EV}-NO": {"ticker": f"{EV}-NO", "event_ticker": EV, "status": "active", "yes_sub_title": "New Orleans", "yes_bid_dollars": "0.4000", "yes_ask_dollars": "0.4200"}}
    http.body = [ev, {"id": "e2", "commence_time": iso, "home_team": "Dallas Cowboys", "away_team": "New York Giants", "bookmakers": [book("dk", "Dallas Cowboys", 1.9, "New York Giants", 1.9)]}]
    r = fv.poll("americanfootball_nfl")
    board = {b["ticker"]: b for b in fv.status()["board"]}
    check("board rows say what they are", board[f"{EV}-GB"]["market"] == "winner" and board[f"{EV}-GB"]["label"] == "Green Bay Packers to win" and board[f"{EV}-GB"]["side"] == "yes")
    gb = board[f"{EV}-GB"]
    check("the game is matched to Kalshi's two markets by team name and date; an unlisted game is ignored", r["results"][0]["matched"] == 1 and r["results"][0]["games"] == 2 and len(board) == 2, r)
    check("edge after the taker fee: fair - ask - 7% x ask x (1 - ask)", close(gb["edge_taker"], fair - 0.59 - 0.07 * 0.59 * 0.41) and close(gb["edge_rest"], fair - 0.58), gb)
    picks = fv.status()["picks"]
    check("Kalshi 59c against a fair 64c: a paper pick is recorded for Green Bay only", len(picks) == 1 and picks[0]["label"] == H + " to win" and close(picks[0]["price"], 0.59)
          and close(picks[0]["fee"], 0.07 * 0.59 * 0.41) and picks[0]["status"] == "open" and picks[0]["tier"] == "edge", picks)
    check("one credit was counted and the key never appears in what is stored", fv.usage()["used"] == 1 and fv.usage()["provider_remaining"] == "498"
          and http.calls[0][1]["markets"] == "h2h" and http.calls[0][1]["regions"] == "us" and "kkkk" not in str(fv.status()), fv.usage())
    fv.poll("americanfootball_nfl")
    check("the same market is not picked twice", len(fv.status()["picks"]) == 1 and fv.usage()["used"] == 2)
    # filters: too few books / books disagree / wide Kalshi spread / game about to start -> no pick
    for name, mut in (("fewer than 3 books", lambda e, m: e.update(bookmakers=e["bookmakers"][:2])),
                      ("books far apart", lambda e, m: e["bookmakers"].append(book("x", H, 1.20, A, 4.5))),
                      ("Kalshi spread too wide", lambda e, m: m.update(yes_bid_dollars="0.5000")),
                      ("game starts in 5 minutes", lambda e, m: e.update(commence_time=dt.datetime.fromtimestamp(time.time() + 300, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))):
        wdb.execute("DELETE FROM fv_picks2")
        import copy
        e2, m2 = copy.deepcopy(ev), dict(k.mk[f"{EV}-GB"])
        mut(e2, m2)
        old = k.mk[f"{EV}-GB"]
        k.mk[f"{EV}-GB"] = m2
        fv.scan_sport("americanfootball_nfl", {"series": "KXNFLGAME", "markets": "h2h"}, [e2])
        k.mk[f"{EV}-GB"] = old
        check(f"no pick when {name}", not wdb.query("SELECT 1 FROM fv_picks2"), wdb.query("SELECT label, edge FROM fv_picks2"))
    # budget: cap 3 -> the next real pull is the last one allowed, then it refuses before calling
    n0 = len(http.calls)
    fv.poll("americanfootball_nfl")
    r = fv.poll("americanfootball_nfl")
    check("the monthly credit cap stops the scanner BEFORE a request is sent", fv.usage()["used"] == 3 and len(http.calls) == n0 + 1 and "budget reached" in (r["error"] or ""), (fv.usage(), r["error"]))
    cfg["fv_monthly_cap"] = 450
    # closing line and settlement
    pk = wdb.query("SELECT * FROM fv_picks2")[0]
    k.mk[f"{EV}-GB"].update(yes_bid_dollars="0.6200", yes_ask_dollars="0.6400")
    fv.upkeep()
    check("before kick-off the latest Kalshi mid is tracked (63c)", close(wdb.query("SELECT last_mid FROM fv_picks2")[0]["last_mid"], 0.63))
    wdb.execute("UPDATE fv_picks2 SET commence_ts=?", (time.time() - 60,))
    k.mk[f"{EV}-GB"].update(yes_bid_dollars="0.9000", yes_ask_dollars="0.9200")           # in-game prices must not count as the close
    fv.upkeep()
    row = wdb.query("SELECT * FROM fv_picks2")[0]
    check("at kick-off the last pre-game mid is frozen as the closing line (63c, not the in-game 91c)", row["status"] == "closed" and close(row["close_mid"], 0.63), row)
    k.mk[f"{EV}-GB"].update(result="yes", status="finalized")
    fv.upkeep()
    row = wdb.query("SELECT * FROM fv_picks2")[0]
    s = fv.summary()
    check("settlement: won $1 for 59c plus the fee", row["status"] == "settled" and row["result"] == "won" and close(row["pnl"], 1 - 0.59 - pk["fee"]) and s["pnl"]["wins"] == 1, row)
    check("closing-line value: bought at 59c, closed at 63c = +4c", close(s["clv"]["mean_c"], 4.0, 0.01) and s["clv"]["beat_close_pct"] == 100.0 and s["clv"]["n"] == 1, s)
    check("it only ever read markets from Kalshi (no order calls exist on the client it is given)", {c_[0] for c_ in k.calls} == {"markets"})
    # ---- spreads and totals: compared only at the exact line the books quote, on both sides; the lean tier
    wdb.execute("DELETE FROM fv_picks2")
    wdb.execute("DELETE FROM fv_board2")
    cfg["fv_sports"] = {"americanfootball_nfl": {"series": "KXNFLGAME", "markets": "h2h,spreads,totals"}}

    def sb(key, hp, ap, pt, op, up, tot):
        return {"key": key, "markets": [{"key": "h2h", "outcomes": [{"name": H, "price": 1.50}, {"name": A, "price": 2.70}]},
                                        {"key": "spreads", "outcomes": [{"name": H, "price": hp, "point": -pt}, {"name": A, "price": ap, "point": pt}]},
                                        {"key": "totals", "outcomes": [{"name": "Over", "price": op, "point": tot}, {"name": "Under", "price": up, "point": tot}]}]}
    ev3 = {"id": "e3", "commence_time": iso, "home_team": H, "away_team": A,
           "bookmakers": [sb("dk", 1.91, 1.91, 3.5, 1.87, 1.95, 44.5), sb("fd", 1.95, 1.87, 3.5, 1.91, 1.91, 44.5), sb("mgm", 1.91, 1.91, 3.5, 1.91, 1.91, 44.5),
                          sb("cz", 2.00, 1.83, 4.5, 1.91, 1.91, 45.5)]}
    sl, tl = FV.spread_lines(ev3), FV.total_lines(ev3)
    p35 = sorted([0.5, (1 / 1.95) / (1 / 1.95 + 1 / 1.87), 0.5])[1]
    check("spreads: each book counts only at the line it quotes (3 books at -3.5, 1 at -4.5)", sl[(H, 3.5)]["books"] == 3 and close(sl[(H, 3.5)]["p"], p35) and sl[(H, 4.5)]["books"] == 1 and (A, 3.5) not in sl, sl)
    check("totals: fair chance of the Over per line", tl[44.5]["books"] == 3 and close(tl[44.5]["p"], 0.5) and 45.5 in tl, tl)
    SP, TO = f"KXNFLSPREAD-{tag}NOGB", f"KXNFLTOTAL-{tag}NOGB"
    k.mk = {f"{EV}-GB": {"ticker": f"{EV}-GB", "event_ticker": EV, "status": "active", "yes_sub_title": "Green Bay", "yes_bid_dollars": "0.6300", "yes_ask_dollars": "0.6500"},
            f"{EV}-NO": {"ticker": f"{EV}-NO", "event_ticker": EV, "status": "active", "yes_sub_title": "New Orleans", "yes_bid_dollars": "0.3500", "yes_ask_dollars": "0.3700"},
            # Green Bay -3.5 is fair at 50%. Kalshi: YES 53/55 -> NO (New Orleans +3.5) costs 47c: 3c under fair, 1.3c after the fee = a lean
            f"{SP}-GB3": {"ticker": f"{SP}-GB3", "event_ticker": SP, "status": "active", "yes_sub_title": "Green Bay wins by over 3.5 points", "floor_strike": 3.5, "yes_bid_dollars": "0.5300", "yes_ask_dollars": "0.5500"},
            f"{SP}-GB7": {"ticker": f"{SP}-GB7", "event_ticker": SP, "status": "active", "yes_sub_title": "Green Bay wins by over 7.5 points", "floor_strike": 7.5, "yes_bid_dollars": "0.2000", "yes_ask_dollars": "0.2200"},
            # Over 44.5 is fair at 50%. Kalshi: 44/46 -> Over costs 46c: 4c under fair, 2.3c after the fee = an edge pick
            f"{TO}-44": {"ticker": f"{TO}-44", "event_ticker": TO, "status": "active", "yes_sub_title": "Over 44.5 points", "floor_strike": 44.5, "yes_bid_dollars": "0.4400", "yes_ask_dollars": "0.4600"},
            f"{TO}-50": {"ticker": f"{TO}-50", "event_ticker": TO, "status": "active", "yes_sub_title": "Over 50.5 points", "floor_strike": 50.5, "yes_bid_dollars": "0.2500", "yes_ask_dollars": "0.2700"}}
    http.status, http.body = 200, [ev3]
    u0 = fv.usage()["used"]
    r = fv.poll("americanfootball_nfl")["results"][0]
    check("winner + spread + total costs three credits", fv.usage()["used"] == u0 + 3 and http.calls[-1][1]["markets"] == "h2h,spreads,totals")
    bd = {(b["ticker"], b["side"]): b for b in fv.status()["board"]}
    check("only Kalshi lines the books actually quote are compared (-3.5 and 44.5; not -7.5 or 50.5)", r["spread_lines"] == 1 and r["total_lines"] == 1
          and (f"{SP}-GB7", "yes") not in bd and (f"{TO}-50", "yes") not in bd, r)
    no35 = bd[(f"{SP}-GB3", "no")]
    check("the other side of a spread is Kalshi's NO: New Orleans +3.5 costs 1 - 53c = 47c", no35["label"] == "New Orleans Saints +3.5" and close(no35["ask"], 0.47) and close(no35["bid"], 0.45)
          and close(no35["fair"], 1 - p35) and close(no35["edge_taker"], (1 - p35) - 0.47 - 0.07 * 0.47 * 0.53), no35)
    pk2 = {(p_["ticker"], p_["side"]): p_ for p_ in fv.status()["picks"]}
    check("tiers: Over 44.5 at 46c is an edge pick; New Orleans +3.5 at 47c is a lean; nothing else is recorded",
          pk2[(f"{TO}-44", "yes")]["tier"] == "edge" and pk2[(f"{SP}-GB3", "no")]["tier"] == "lean" and len(pk2) == 2 and r["picks"] == 1 and r["leans"] == 1, {k_: v_["tier"] for k_, v_ in pk2.items()})
    k.mk[f"{SP}-GB3"].update(yes_bid_dollars="0.5000", yes_ask_dollars="0.5200")
    fv.upkeep()
    check("a NO pick is tracked at the NO price (1 - 51c = 49c)", close(wdb.query("SELECT last_mid FROM fv_picks2 WHERE side='no'")[0]["last_mid"], 0.49))
    wdb.execute("UPDATE fv_picks2 SET commence_ts=?", (time.time() - 60,))
    k.mk[f"{SP}-GB3"].update(result="no")
    k.mk[f"{TO}-44"].update(result="no")
    fv.upkeep()
    rr = {(x["ticker"], x["side"]): x for x in wdb.query("SELECT * FROM fv_picks2")}
    check("settlement by side: the NO pick wins when the market resolves NO, the Over pick loses", rr[(f"{SP}-GB3", "no")]["result"] == "won" and rr[(f"{SP}-GB3", "no")]["pnl"] > 0.5
          and rr[(f"{TO}-44", "yes")]["result"] == "lost" and close(rr[(f"{TO}-44", "yes")]["pnl"], -0.46 - 0.07 * 0.46 * 0.54), rr)
    s2 = fv.summary()
    check("results are reported per tier and per market type", s2["tiers"]["edge"]["picks"] == 1 and s2["tiers"]["lean"]["picks"] == 1 and s2["markets"]["spread"]["pnl"]["wins"] == 1 and close(s2["tiers"]["lean"]["clv"]["mean_c"], 2.0, 0.01), s2["tiers"])
    k.mk = {}
    u1 = fv.usage()["used"]
    r = fv.poll("americanfootball_nfl")["results"][0]
    check("no Kalshi game in the next two days -> the pull is skipped and costs nothing", "skipped" in r and fv.usage()["used"] == u1, r)
    k.mk = {f"{EV}-GB": {"ticker": f"{EV}-GB", "event_ticker": EV, "status": "active", "yes_sub_title": "Green Bay", "yes_bid_dollars": "0.5700", "yes_ask_dollars": "0.5900"},
            f"{EV}-NO": {"ticker": f"{EV}-NO", "event_ticker": EV, "status": "active", "yes_sub_title": "New Orleans", "yes_bid_dollars": "0.4000", "yes_ask_dollars": "0.4200"}}
    fv.db.put("fv_last_by_sport", {"americanfootball_nfl": time.time()})
    check("each sport waits for its own interval", not fv.due() and fv.poll(force=False)["results"] == [])
    cfg["theodds_key"] = ""
    check("without a key nothing is pulled", not fv.configured and not fv.due())
    http.status, http.body = 401, {"message": "bad key " + "k" * 32}
    cfg["theodds_key"] = "k" * 32
    r = fv.poll("americanfootball_nfl")
    check("an API error is reported with the key removed", r["error"] and "401" in r["error"] and "kkkk" not in r["error"] and "kkkk" not in str(fv.status()["calls"]), r["error"])
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
