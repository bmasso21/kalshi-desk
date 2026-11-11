"""Phase 2.2 tests: OddsPapi budget (hard monthly cap), key handling (never shown/logged/audited), discovery."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["KALSHI_DESK_NO_WATCHDOG"] = "1"

from app import sports as S  # noqa: E402
from app.warehouse import WarehouseDB  # noqa: E402

OK, FAIL = [], []
KEY = "TESTKEY_abcdefghijklmnop123456"


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


class Resp:
    def __init__(self, code, js):
        self.status_code, self._js = code, js
        self.text = json.dumps(js)
        self.content = self.text.encode()

    def json(self):
        return self._js

    def iter_content(self, n):
        for i in range(0, len(self.content), n):
            yield self.content[i:i + n]

    def close(self):
        pass


class Sess:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None, stream=False):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        p = url.split("/v4", 1)[1]
        if params.get("apiKey") != KEY:
            return Resp(401, {"error": 401, "message": "invalid apiKey"})
        if p == "/sports":
            return Resp(200, [{"sportId": 11, "slug": "basketball", "sportName": "Basketball"},
                              {"sportId": 13, "slug": "american-football", "sportName": "American Football"},
                              {"sportId": 15, "slug": "ice-hockey", "sportName": "Ice Hockey"},
                              {"sportId": 14, "slug": "baseball", "sportName": "Baseball"},
                              {"sportId": 10, "slug": "soccer", "sportName": "Soccer"}])
        if p == "/tournaments":
            name = {11: "NBA", 13: "NFL", 15: "NHL", 14: "MLB"}[params["sportId"]]
            return Resp(200, [{"tournamentId": 100 + params["sportId"], "tournamentName": name, "sportId": params["sportId"]},
                              {"tournamentId": 999, "tournamentName": "Other"}])
        if p == "/fixtures":
            return Resp(200, [{"fixtureId": "id1", "participant1Name": "A", "statusId": 2}, {"fixtureId": "id2", "statusId": 0}])
        if p == "/historical-odds":
            return Resp(200, {"id1": {"bookmakers": {"pinnacle": {"markets": {"101": {}}}}}})
        if p == "/markets":
            return Resp(200, [{"marketId": 1, "marketName": "Moneyline"}])
        if p == "/bookmakers":
            return Resp(200, [{"slug": "pinnacle"}])
        return Resp(404, {"error": "nope " + str(params)})


class KC:
    def series_list(self, category=None):
        return [{"ticker": "KXNFLGAME", "title": "NFL game", "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1},
                {"ticker": "KXNBAGAME", "title": "NBA game"}, {"ticker": "KXOTHER"}]


def study_tests(tmp):
    import datetime as dt
    db = WarehouseDB(Path(tmp) / "study.db")
    conf = {"oddspapi_key": KEY, "monthly_cap": 240}
    T0 = int(time.time()) - 5 * 86400            # a finished game 5 days ago, 00:20 UTC-ish
    T0 -= T0 % 3600
    et = dt.datetime.fromtimestamp(T0 - 5 * 3600, dt.timezone.utc).date()
    code = et.strftime("%y") + et.strftime("%b").upper() + et.strftime("%d")
    ev = f"KXNFLGAME-{code}LACPIT"

    def snaps(base, n=30):     # price history: p1 (LAC) favourite, drifting
        return [{"price": round(base + 0.01 * i, 3), "active": True,
                 "createdAt": dt.datetime.fromtimestamp(T0 - (n - i) * 1800, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")} for i in range(n)]

    class OS:
        def __init__(self):
            self.calls, self.r429 = [], 0

        def get(self, url, params=None, headers=None, timeout=None, stream=False):
            self.calls.append((url, dict(params or {})))
            p = url.split("/v4", 1)[1]
            if p == "/sports":
                return Resp(200, [{"sportId": 14, "slug": "american-football"}, {"sportId": 11, "slug": "basketball"}])
            if p == "/tournaments":
                return Resp(200, [{"tournamentId": 31, "tournamentSlug": "nfl", "tournamentName": "NFL"}, {"tournamentId": 32, "tournamentSlug": "ncaa"}])
            if p == "/markets":
                return Resp(200, [{"marketId": 140, "marketName": "Spread", "handicap": -3.5, "outcomes": [{"outcomeId": 1}, {"outcomeId": 2}]},
                                  {"marketId": 141, "marketName": "Moneyline (incl. overtime)", "outcomes": [{"outcomeId": 1412, "outcomeName": "2"}, {"outcomeId": 1411, "outcomeName": "1"}]},
                                  {"marketId": 142, "marketName": "Total", "outcomes": [{"outcomeId": 1}, {"outcomeId": 2}, {"outcomeId": 3}]}])
            if p == "/fixtures":
                return Resp(200, [{"fixtureId": "idA", "participant1Name": "Los Angeles Chargers", "participant2Name": "Pittsburgh Steelers",
                                   "participant1Abbr": "LAC", "participant2Abbr": "PIT", "statusId": 2,
                                   "startTime": dt.datetime.fromtimestamp(T0, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")},
                                  {"fixtureId": "idB", "participant1Name": "Nowhere Ones", "participant2Name": "Elsewhere Twos", "statusId": 2,
                                   "startTime": dt.datetime.fromtimestamp(T0, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")}])
            if p == "/historical-odds":
                if self.r429 == 0:
                    self.r429 += 1
                    return Resp(429, {"error": {"code": "RATE_LIMITED", "retryMs": 100}})
                return Resp(200, {params["fixtureId"]: {"bookmakers": {"pinnacle": {"markets": {"141": {"outcomes": {
                    "1411": {"players": {"0": snaps(1.50)}}, "1412": {"players": {"0": snaps(2.70)}}}},
                    "140": {"outcomes": {"1": {"players": {"0": snaps(1.9)}}}}}},
                    "kalshi": {"markets": {"141": {"outcomes": {"1411": {"players": {"0": snaps(1.4)}}}}}}}}})
            return Resp(404, {})

    class KC:
        def __init__(self):
            self.req = []

        def paginate(self, path, key, params=None, auth=False, max_items=None):
            if path == "/markets":
                if params.get("status") != "settled":
                    return iter([])
                return iter([{"ticker": f"{ev}-LAC", "event_ticker": ev, "yes_sub_title": "Los Angeles C", "result": "yes", "status": "finalized",
                              "close_time": dt.datetime.fromtimestamp(T0 + 4 * 3600, dt.timezone.utc).isoformat()},
                             {"ticker": f"{ev}-PIT", "event_ticker": ev, "yes_sub_title": "Pittsburgh", "result": "no", "status": "finalized",
                              "close_time": dt.datetime.fromtimestamp(T0 + 4 * 3600, dt.timezone.utc).isoformat()},
                             {"ticker": "KXNFLGAME-26JAN01AAABBB-AAA", "event_ticker": "KXNFLGAME-26JAN01AAABBB", "yes_sub_title": "A"}])
            if path in ("/markets/trades", "/historical/trades"):
                t = params["ticker"]
                yes_side = t.endswith("LAC")
                return iter([{"trade_id": f"{t}-{i}", "ticker": t, "created_time": dt.datetime.fromtimestamp(T0 - 3 * 3600 + i * 60, dt.timezone.utc).isoformat(),
                              "yes_price_dollars": "0.55" if yes_side else "0.45", "count_fp": "40", "taker_side": "no"} for i in range(150)])
            return iter([])

        def request(self, method, path, params=None):
            self.req.append(path)
            t = path.split("/markets/")[1].split("/")[0]
            lo = 0.60 if t.endswith("LAC") else 0.36
            cs = [{"end_period_ts": ts, "yes_bid": {"close_dollars": f"{lo:.2f}"}, "yes_ask": {"close_dollars": f"{lo + 0.04:.2f}"},
                   "price": {"close_dollars": f"{lo + 0.02:.2f}"}, "volume_fp": "5"} for ts in range(max(params["start_ts"], T0 - 7 * 3600) // 60 * 60, min(params["end_ts"], T0 + 3600), 60)]
            return {"candlesticks": cs}

        def series(self, t):
            return {"ticker": t, "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}

    class WH:
        def cutoff(self):
            return {}

    os_ = OS()
    op = S.OddsPapi(lambda: conf, db, session=os_)
    slept = []
    op.sleep = lambda s: slept.append(round(s, 2))
    st = S.Study(op, db, KC(), WH())
    res = st.run(("NFL",), 21, 10)
    log = "\n".join(res["log"])
    fx = db.query("SELECT * FROM sp_fixtures ORDER BY fixture_id")
    check("study: fixtures stored, Kalshi match by abbreviation", fx[0]["kalshi_event"] == ev and fx[0]["p1_market"].endswith("-LAC")
          and fx[0]["match_note"] == "abbr" and fx[1]["match_note"] == "no Kalshi match", ([dict(x) for x in fx], log))
    md = db.get("sp_meta")["NFL"]
    check("study: moneyline market chosen (not spread/total)", md["market_id"] == "141" and sorted(o["id"] for o in md["outcomes"]) == ["1411", "1412"], md)
    hist_calls = [c for c in os_.calls if c[0].endswith("/historical-odds")]
    check("study: history pulled only for the matched game (+1 retry after 429), Pinnacle only", len(hist_calls) == 2
          and all(c[1]["fixtureId"] == "idA" and c[1]["bookmakers"] == "pinnacle" for c in hist_calls))
    check("study: 429 -> waited retryMs then retried", any(0.5 <= x <= 1.0 for x in slept), slept)
    check("study: request count = sports+tournaments+markets+fixtures+2 history", op.usage()["used"] == 6, op.usage())
    n_odds = db.query("SELECT count(*) n FROM sp_odds")[0]["n"]
    check("study: only Pinnacle moneyline snapshots stored", n_odds == 60, n_odds)
    smp = op.raw("hist_sample")["body"]
    check("study: raw sample is size-capped", len(smp["head"]) <= 20000 and smp["bytes"] > 0 and smp["moneyline_rows"] == 60)
    nc = db.query("SELECT count(*) n FROM wh_candles WHERE period=1")[0]["n"]
    nt = db.query("SELECT count(*) n FROM wh_trades")[0]["n"]
    check("study: Kalshi 1-min candles + trades synced", nc > 400 and nt == 300, (nc, nt))
    check("study: series fee type stored", db.query("SELECT fee_type FROM wh_series WHERE series_ticker='KXNFLGAME'")[0]["fee_type"] == "quadratic_with_maker_fees")
    db.executemany("INSERT INTO wh_trades VALUES(?,?,?,?,?,?,?,?)", [(f"ingame{i}", f"{ev}-LAC", T0 + 600 * (i + 1), 0.5, 10, "yes", "ask", 0) for i in range(5)])
    db.execute("UPDATE sp_fixtures SET kalshi_synced=NULL WHERE fixture_id='idA'")
    res2 = st.run(("NFL",), 21, 10)
    check("study: in-game trades cleaned up, unfinished Kalshi sync resumed (no OddsPapi request)",
          db.query("SELECT count(*) n FROM wh_trades WHERE ts > ?", (T0,))[0]["n"] == 0
          and db.query("SELECT kalshi_synced FROM sp_fixtures WHERE fixture_id='idA'")[0]["kalshi_synced"] is not None
          and any("cleanup: removed 5" in x for x in res2["log"]), res2["log"])
    check("study: re-run doesn't re-pull the same game", len([c for c in os_.calls if c[0].endswith("/historical-odds")]) == 2)
    # pacing
    op2 = S.OddsPapi(lambda: conf, db, session=os_)
    sl2 = []
    op2.sleep = lambda s: sl2.append(s)
    op2._last["/historical-odds"] = time.time()
    try:
        op2.get("/historical-odds", {"fixtureId": "idA"})
    except Exception:
        pass
    check("pacing: waits out the 5 s historical cooldown before sending", sl2 and 4.8 <= sl2[0] <= 5.4, sl2)

    # oversized response -> per-outcome fallback
    class Big(OS):
        def get(self, url, params=None, headers=None, timeout=None, stream=False):
            if url.endswith("/historical-odds") and "outcomeId" not in params:
                self.calls.append((url, dict(params or {})))
                return Resp(200, {"x": "y" * 3000})
            return super().get(url, params, headers, timeout)
    db.execute("UPDATE sp_fixtures SET hist_pulled=NULL WHERE fixture_id='idA'")
    db.execute("DELETE FROM sp_odds")
    bg = Big()
    bg.r429 = 1
    opb = S.OddsPapi(lambda: conf, db, session=bg)
    opb.sleep = lambda s: None
    stb = S.Study(opb, db, KC(), WH())
    import app.sports as SM
    real = SM.OddsPapi.get

    def small_cap(self, path, params=None, note="", max_bytes=None):
        return real(self, path, params, note, 1000 if max_bytes and "outcomeId" not in (params or {}) else max_bytes)
    SM.OddsPapi.get = small_cap
    try:
        n = stb.pull_history(db.query("SELECT * FROM sp_fixtures WHERE fixture_id='idA'")[0])
    finally:
        SM.OddsPapi.get = real
    oc = [c[1].get("outcomeId") for c in bg.calls if c[0].endswith("/historical-odds") and "outcomeId" in c[1]]
    check("too-big response aborted early, then one request per moneyline outcome", sorted(oc) == ["1411", "1412"] and n == 60, (oc, n))
    check("schema created once per process (no CREATE on every screen refresh)", len([k for k in SM._SCHEMA_DONE if k[0] == id(db)]) == 2)

    # model: no-vig + lag
    m = S.SportsModel(db, lag_sec=60)
    t = T0 - 1800 * 5 + 30            # 30 s after a snapshot -> with 60 s lag the previous snapshot is used
    fv = m.fair({"ticker": f"{ev}-LAC"}, t)
    o1, o2 = 1.50 + 0.01 * 24, 2.70 + 0.01 * 24     # snapshot i=24 is the last one older than t-60
    exp = (1 / o1) / (1 / o1 + 1 / o2)
    check("model: Pinnacle no-vig probability, lagged; outcome '1' = participant 1 even when listed second", fv and abs(fv["p_yes"] - exp) < 1e-9, (fv, exp))
    fv2 = m.fair({"ticker": f"{ev}-PIT"}, t)
    check("model: other team = 1 - p", abs(fv2["p_yes"] - (1 - exp)) < 1e-9)
    check("model: nothing before the first snapshot", m.fair({"ticker": f"{ev}-LAC"}, T0 - 40 * 3600) is None)
    ev_ = S.evaluate(db, leads_min=(120, 30))
    check("eval: scores Kalshi vs Pinnacle at fixed leads", ev_["by_lead"]["120"]["n"] == 2 and ev_["by_lead"]["120"]["agreement_check"] > 0, ev_)

    # backtest
    from app.backtest import Backtester
    r = Backtester(db).run("sports_fair", {"edge": 0.0, "start_minutes": 400, "every_min": 5})
    th = r["models"]["touch"]
    check("backtest sports_fair: runs on 1-min bars of matched markets", r["filters"]["series"] == ["KXNFLGAME"] and th["markets_simulated"] == 2, r["filters"])
    check("backtest sports_fair: maker fills pay sports maker fees", th["fills"] > 0 and th["fees"] > 0, th)
    r0 = Backtester(db).run("sports_fair", {"edge": 0.5})
    check("backtest sports_fair: huge edge -> no fills", r0["models"]["through"]["fills"] == 0)
    db.conn.close()


def main():
    tmp = tempfile.mkdtemp()
    db = WarehouseDB(Path(tmp) / "w.db")
    conf = {"oddspapi_key": KEY, "monthly_cap": 5}
    sess = Sess()
    op = S.OddsPapi(lambda: conf, db, session=sess)
    check("usage starts at 0", op.usage()["used"] == 0 and op.usage()["cap"] == 5)
    op.get("/sports")
    check("v4 API with the apiKey query parameter", sess.calls[0][0] == "https://api.oddspapi.io/v4/sports" and sess.calls[0][1].get("apiKey") == KEY)
    check("each request counted", op.usage()["used"] == 1)
    for _ in range(4):
        op.get("/sports")
    n = len(sess.calls)
    try:
        op.get("/sports")
        blocked = False
    except S.BudgetExceeded:
        blocked = True
    check("hard cap: 6th request refused and NOT sent", blocked and len(sess.calls) == n and op.usage()["used"] == 5)
    db.put("sp_usage", {"month": "2000-01", "count": 999})
    check("new month resets the count", op.usage()["used"] == 0)
    try:
        op.get("/nothing")
        err = ""
    except RuntimeError as e:
        err = str(e)
    check("HTTP errors raised with the key redacted", "404" in err and KEY not in err and "***" in err, err)

    class Boom:
        def get(self, url, params=None, headers=None, timeout=None, stream=False):
            raise ConnectionError(f"failed to reach {url}?apiKey={params.get('apiKey')}")
    db.put("sp_usage", {"month": op.month(), "count": 0})
    opb = S.OddsPapi(lambda: conf, db, session=Boom())
    try:
        opb.get("/sports")
        e2 = ""
    except RuntimeError as e:
        e2 = str(e)
    check("network errors never leak the key (exception + call log)", e2 and KEY not in e2 and KEY not in json.dumps(opb.calls()), e2)
    check("call log never contains the key", KEY not in json.dumps(op.calls()))
    conf2 = {"oddspapi_key": "", "monthly_cap": 5}
    op2 = S.OddsPapi(lambda: conf2, db, session=Sess())
    try:
        op2.get("/sports")
        nokey = False
    except RuntimeError as e:
        nokey = "Settings" in str(e)
    check("no key -> refuses without spending", nokey and op2.usage()["used"] == 1)   # (counter shared: 1 from the failed call above)

    # discovery
    db.put("sp_usage", {"month": op.month(), "count": 0})
    conf["monthly_cap"] = 240
    sess2 = Sess()
    op3 = S.OddsPapi(lambda: conf, db, session=sess2)
    d = S.discover(op3, KC())
    check("discovery finds sport + league ids", d["sport_ids"] == {"basketball": 11, "american-football": 13, "ice-hockey": 15, "baseball": 14}
          and d["tournament_ids"] == {"NBA": 111, "NFL": 113, "NHL": 115, "MLB": 114}, (d["sport_ids"], d["tournament_ids"]))
    check("discovery stays within its request budget (<= 9)", len(sess2.calls) <= 9 and op3.usage()["used"] == len(sess2.calls), len(sess2.calls))
    check("discovery pulls one finished NFL game's history (Pinnacle + prediction markets)",
          any(c[0].endswith("/historical-odds") and c[1].get("fixtureId") == "id1" and "pinnacle" in c[1].get("bookmakers", "") for c in sess2.calls))
    check("raw responses stored for review", "id1" in op3.raw("discover:historical:NFL")["body"])
    check("Kalshi game series listed", [s["ticker"] for s in d["kalshi"]["game_series"]] == ["KXNFLGAME", "KXNBAGAME"])
    conf["monthly_cap"] = op3.usage()["used"] + 3
    d2 = S.discover(op3, None)
    check("discovery stops cleanly at the cap", "budget" in (d2.get("stopped") or "") and op3.usage()["used"] <= conf["monthly_cap"], d2.get("stopped"))
    conf["monthly_cap"] = 240
    bad = {"oddspapi_key": "WRONGKEY_abcdefghijklmnop", "monthly_cap": 240}
    sess3 = Sess()
    d3 = S.discover(S.OddsPapi(lambda: bad, db, session=sess3), None)
    check("invalid key: discovery stops after the first request", len(sess3.calls) == 1 and "401" in (d3.get("stopped") or ""), (len(sess3.calls), d3.get("stopped")))

    # API + config handling
    from fastapi.testclient import TestClient

    from app.config import Config
    from app.db import DB
    from app.server import create_app
    cfg = Config(Path(tmp) / "config.json")
    app = create_app(cfg, DB(Path(tmp) / "desk.db"), start_engine=False)
    c = TestClient(app)
    tok = c.get("/api/session").json()["token"]
    H = {"X-Desk-Token": tok}
    check("sports status: not configured", c.get("/api/sports").json()["configured"] is False)
    check("discover refused without a key", c.post("/api/sports/discover", headers=H).status_code == 400)
    r = c.post("/api/config", headers=H, json={"sports": {"oddspapi_key": "bad key!"}})
    check("invalid key format rejected", r.status_code == 400 and not cfg.data["sports"]["oddspapi_key"])
    r = c.post("/api/config", headers=H, json={"sports": {"oddspapi_key": KEY, "monthly_cap": 999}})
    check("valid key saved; cap clamped to 250", r.status_code == 200 and cfg.data["sports"]["oddspapi_key"] == KEY
          and cfg.data["sports"]["monthly_cap"] == 250)
    pub = json.dumps(c.get("/api/config").json(), ensure_ascii=False)
    check("key never returned to the browser (masked)", KEY not in pub and "TEST…" in pub)
    r = c.post("/api/config", headers=H, json={"sports": {"oddspapi_key": "TEST…"}})
    check("masked value posted back keeps the stored key", cfg.data["sports"]["oddspapi_key"] == KEY)
    aud = json.dumps(app.state.engine.db.query("SELECT * FROM audit"))
    check("key never written to the audit trail", KEY not in aud and "monthly_cap" in aud)
    check("sports status: configured", c.get("/api/sports").json()["configured"] is True)
    check("discover needs the CSRF token", c.post("/api/sports/discover").status_code in (401, 403))
    check("version 2.3.4", __import__("app").__version__ == "2.3.4")
    check("no orders placed", not app.state.engine.db.query("SELECT 1 FROM orders_log LIMIT 1"))


if __name__ == "__main__":
    main()
    study_tests(tempfile.mkdtemp())
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)
