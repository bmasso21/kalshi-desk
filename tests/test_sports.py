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


class Sess:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
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
        def get(self, url, params=None, headers=None, timeout=None):
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
    check("version 2.2.1", __import__("app").__version__ == "2.2.1")
    check("no orders placed", not app.state.engine.db.query("SELECT 1 FROM orders_log LIMIT 1"))


if __name__ == "__main__":
    main()
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)
