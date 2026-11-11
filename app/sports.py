"""Sports fair value research (Phase 2.2): Pinnacle line history from OddsPapi (free tier) vs Kalshi game markets.

OddsPapi free plan = 250 requests / month, so every call goes through a hard budget:
  - counted locally per calendar month (UTC) BEFORE the request is sent, persisted in the warehouse
  - refused once `monthly_cap` is reached (default 240, leaving a little headroom)
  - every call is logged (path + params, never the key)
The key lives only in config.json (entered by Brian in Settings), is sent as the v4 `apiKey` parameter, and is
redacted from every error and log line; it is never returned to the browser or written to the audit trail.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
import time

import requests

ODDSPAPI = "https://api.oddspapi.io/v4"     # free-signup keys are for v4 (v5 rejects them)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sp_calls (ts REAL, path TEXT, params TEXT, status INTEGER, bytes INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS sp_raw (k TEXT PRIMARY KEY, ts REAL, body TEXT);
"""


# per-endpoint cooldowns from the OddsPapi v4 docs (+ a margin)
COOLDOWN = {"/sports": 1.2, "/tournaments": 1.2, "/markets": 1.2, "/fixtures": 2.3, "/historical-odds": 5.3}


class BudgetExceeded(Exception):
    pass


class OddsPapi:
    def __init__(self, cfg_getter, wdb, session=None, base=None):
        self._cfg = cfg_getter          # callable -> the "sports" config section (read fresh each call)
        self.db = wdb
        self.db.conn.executescript(SCHEMA)
        self.db.conn.commit()
        self.http = session or requests.Session()
        self._base = base
        self.lock = threading.Lock()
        self._last: dict[str, float] = {}
        self.sleep = time.sleep

    # ---- budget ----
    @staticmethod
    def month(now=None) -> str:
        return dt.datetime.fromtimestamp(now or time.time(), dt.timezone.utc).strftime("%Y-%m")

    def usage(self) -> dict:
        u = self.db.get("sp_usage") or {}
        m = self.month()
        used = u.get("count", 0) if u.get("month") == m else 0
        cap = int(self._cfg().get("monthly_cap", 240))
        return {"month": m, "used": used, "cap": cap, "left": max(0, cap - used), "plan_limit": 250}

    def _spend(self, n=1):
        with self.lock:
            u = self.usage()
            if u["used"] + n > u["cap"]:
                raise BudgetExceeded(f"OddsPapi monthly budget reached ({u['used']}/{u['cap']} used in {u['month']})")
            self.db.put("sp_usage", {"month": u["month"], "count": u["used"] + n})

    @property
    def base(self) -> str:
        return self._base or self._cfg().get("api_base") or ODDSPAPI

    @property
    def configured(self) -> bool:
        return bool((self._cfg().get("oddspapi_key") or "").strip())

    def get(self, path: str, params: dict | None = None, note: str = "") -> object:
        key = (self._cfg().get("oddspapi_key") or "").strip()
        if not key:
            raise RuntimeError("No OddsPapi key yet - add it in Settings")
        params = {k: v for k, v in (params or {}).items() if v is not None}
        mode = self._cfg().get("auth_mode", "query")          # v4 documents the apiKey query parameter
        q = dict(params, apiKey=key) if mode == "query" else params
        hdr = {"X-API-Key": key} if mode == "header" else {}
        red = lambda t: str(t).replace(key, "***")
        for attempt in range(2):
            self._pace(path)
            self._spend(1)                      # counted before sending: a failed call may still count upstream
            try:
                r = self.http.get(self.base + path, params=q, headers=hdr, timeout=90)
            except Exception as e:
                self.db.execute("INSERT INTO sp_calls VALUES(?,?,?,?,?,?)", (time.time(), path, json.dumps(params), -1, 0, red(f"{note} {e}")[:300]))
                raise RuntimeError(red(f"OddsPapi {path}: {type(e).__name__}: {e}")) from None
            finally:
                self._last[path] = time.time()
            self.db.execute("INSERT INTO sp_calls VALUES(?,?,?,?,?,?)",
                            (time.time(), path, json.dumps(params), r.status_code, len(r.content), note[:200]))
            if r.status_code == 429 and attempt == 0:
                try:
                    ms = float(((r.json() or {}).get("error") or {}).get("retryMs") or 2000)
                except Exception:
                    ms = 2000
                self.sleep(min(30.0, ms / 1000 + 0.5))
                continue
            if r.status_code >= 400:
                raise RuntimeError(red(f"OddsPapi {path} HTTP {r.status_code}: {r.text[:300]}"))
            return r.json()

    def _pace(self, path):
        """OddsPapi enforces a per-endpoint cooldown; wait it out instead of burning a request on a 429."""
        gap = COOLDOWN.get(path, 2.2)
        wait = self._last.get(path, 0) + gap - time.time()
        if wait > 0:
            self.sleep(wait)

    def calls(self, limit=100) -> list[dict]:
        return self.db.query("SELECT * FROM sp_calls ORDER BY ts DESC LIMIT ?", (limit,))

    def save_raw(self, k, body):
        self.db.execute("INSERT OR REPLACE INTO sp_raw VALUES(?,?,?)", (k, time.time(), json.dumps(body)))

    def raw(self, k):
        r = self.db.query("SELECT ts, body FROM sp_raw WHERE k=?", (k,))
        return {"ts": r[0]["ts"], "body": json.loads(r[0]["body"])} if r else None


SPORT_HINTS = {"basketball": "NBA", "american-football": "NFL", "ice-hockey": "NHL", "baseball": "MLB"}


def discover(op: OddsPapi, kalshi_client=None, max_requests: int = 9) -> dict:
    """Step 1 (≈7 requests, v4 API): sports, NBA/NFL/NHL/MLB tournament ids, recent NFL fixtures, and ONE full
    historical-odds pull for a finished NFL game (Pinnacle + Kalshi + Polymarket if offered) to learn the market
    ids and data shape. Raw responses are stored for review. Kalshi side (free, public): game series + fee types."""
    out = {"started": time.time(), "steps": [], "kalshi": {}, "api": op.base}
    start_used = op.usage()["used"]

    def step(name, path, params=None):
        if op.usage()["used"] - start_used >= max_requests:
            out["steps"].append({"step": name, "skipped": "request limit for this run"})
            return None
        try:
            body = op.get(path, params, note=f"discover:{name}")
            op.save_raw(f"discover:{name}", body)
            n = len(body) if isinstance(body, (list, dict)) else None
            out["steps"].append({"step": name, "ok": True, "items": n})
            return body
        except Exception as e:
            out["steps"].append({"step": name, "ok": False, "error": str(e)[:300]})
            if "401" in str(e) or "budget" in str(e):
                raise
            return None

    lst = lambda b: b if isinstance(b, list) else (b.get("data") if isinstance(b, dict) and isinstance(b.get("data"), list) else [])
    try:
        ids = {}
        for s_ in lst(step("sports", "/sports") or []):
            slug = str(s_.get("slug") or "").lower()
            if slug in SPORT_HINTS:
                ids[slug] = s_.get("sportId")
        out["sport_ids"] = ids
        tourn = {}
        for slug, sid in ids.items():
            want = SPORT_HINTS[slug]
            for t in lst(step(f"tournaments:{slug}", "/tournaments", {"sportId": sid}) or []):
                nm, sl = str(t.get("tournamentName") or ""), str(t.get("tournamentSlug") or "")
                if (nm.upper() == want or sl.lower() == want.lower()) and want not in tourn:
                    tourn[want] = t.get("tournamentId")
        out["tournament_ids"] = tourn
        if tourn.get("NFL"):
            now = time.time()
            fmt = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            fx = lst(step("fixtures:NFL", "/fixtures", {"tournamentId": tourn["NFL"], "from": fmt(now - 9 * 86400),
                                                        "to": fmt(now + 3 * 86400)}) or [])
            done = [f for f in fx if f.get("statusId") == 2 or str(f.get("statusName", "")).lower() in ("finished", "ended")]
            out["nfl_fixtures"] = len(fx)
            out["nfl_finished"] = len(done)
            if done:
                f = done[-1]
                out["sample_fixture"] = {k: f.get(k) for k in ("fixtureId", "participant1Name", "participant2Name", "startTime")}
                step("historical:NFL", "/historical-odds", {"fixtureId": f["fixtureId"], "bookmakers": "pinnacle,kalshi,polymarket"})
    except Exception as e:
        out["stopped"] = str(e)[:300]
    if kalshi_client is not None:
        try:
            ser = kalshi_client.series_list(category="Sports")
            cand = [s_ for s_ in ser if (s_.get("ticker") or "") in ("KXNFLGAME", "KXNBAGAME", "KXNHLGAME", "KXMLBGAME", "KXNCAAFGAME")]
            out["kalshi"] = {"sports_series": len(ser),
                             "game_series": [{k: s_.get(k) for k in ("ticker", "title", "fee_type", "fee_multiplier")} for s_ in cand]}
        except Exception as e:
            out["kalshi"] = {"error": str(e)[:300]}
    out["finished"] = time.time()
    out["usage"] = op.usage()
    op.db.put("sp_discover", out)
    return out


# ================================================================== study ==
LEAGUES = {"NBA": ("basketball", "nba", "KXNBAGAME"), "NFL": ("american-football", "nfl", "KXNFLGAME"),
           "NHL": ("ice-hockey", "nhl", "KXNHLGAME"), "MLB": ("baseball", "mlb", "KXMLBGAME")}

STUDY_SCHEMA = """
CREATE TABLE IF NOT EXISTS sp_fixtures (
    fixture_id TEXT PRIMARY KEY, league TEXT, start_ts REAL, status INTEGER, p1 TEXT, p2 TEXT, p1_abbr TEXT, p2_abbr TEXT,
    raw TEXT, kalshi_event TEXT, p1_market TEXT, p2_market TEXT, match_note TEXT, hist_pulled REAL, hist_rows INTEGER,
    kalshi_synced REAL);
CREATE INDEX IF NOT EXISTS sp_fixtures_league ON sp_fixtures(league, start_ts);
CREATE TABLE IF NOT EXISTS sp_odds (
    fixture_id TEXT, bookmaker TEXT, market_id TEXT, outcome_id TEXT, player_id TEXT, ts REAL, price REAL, active INTEGER,
    PRIMARY KEY (fixture_id, bookmaker, market_id, outcome_id, player_id, ts));
"""

TICKER_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})([A-Z0-9]+)$")
_MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}


def _iso(s):
    if s in (None, ""):
        return None
    if isinstance(s, (int, float)):
        return float(s) / (1000 if s > 1e11 else 1)
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _toks(s: str) -> list[str]:
    import re
    return [t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if t]


def name_match(kalshi_name: str, participant: str) -> bool:
    """Every token of Kalshi's short team name ('Los Angeles L') is a prefix of a token in the full name
    ('Los Angeles Lakers'), in order."""
    kt, pt = _toks(kalshi_name), _toks(participant)
    if not kt or not pt:
        return False
    j = 0
    for t in kt:
        while j < len(pt) and not pt[j].startswith(t):
            j += 1
        if j == len(pt):
            return False
        j += 1
    return True


def kalshi_event_parts(event_ticker: str):
    m = TICKER_RE.search(event_ticker or "")
    if not m or m.group(2) not in _MON:
        return None, None
    try:
        return dt.date(2000 + int(m.group(1)), _MON[m.group(2)], int(m.group(3))), m.group(4)
    except ValueError:
        return None, None


def match_fixture(fx: dict, events: dict) -> tuple | None:
    """fx: fixture row. events: {event_ticker: [market dicts]}. -> (event, p1_market, p2_market, note)."""
    start = fx["start_ts"]
    et_date = dt.datetime.fromtimestamp(start - 5 * 3600, dt.timezone.utc).date()
    a1, a2 = (fx.get("p1_abbr") or "").upper(), (fx.get("p2_abbr") or "").upper()
    best = None
    for ev, ms in events.items():
        d, codes = kalshi_event_parts(ev)
        if not d or abs((d - et_date).days) > 1 or len(ms) != 2:
            continue
        m1 = m2 = None
        how = ""
        for m in ms:
            code = (m["ticker"].rsplit("-", 1)[-1]).upper()
            sub = m.get("yes_sub_title") or ""
            if a1 and code == a1 or name_match(sub, fx["p1"]):
                m1 = m1 or m
            elif a2 and code == a2 or name_match(sub, fx["p2"]):
                m2 = m2 or m
        if m1 and m2 and m1 is not m2:
            how = "abbr" if a1 and a2 and codes in (a1 + a2, a2 + a1) else "name"
            score = (0 if d == et_date else 1, 0 if how == "abbr" else 1)
            if best is None or score < best[0]:
                best = (score, ev, m1["ticker"], m2["ticker"], f"{how}{'' if d == et_date else ' date±1'}")
    return best[1:] if best else None


def walk_historical(body, fixture_id) -> list[tuple]:
    """Flatten OddsPapi v4 historical-odds: {fixtureId: {bookmakers: {slug: {markets: {mid: {outcomes: {oid:
    {players: {pid: [ {price, active, createdAt}, ...]}}}}}}}}} -> rows for sp_odds."""
    out = []
    root = body.get(fixture_id) if isinstance(body, dict) and fixture_id in body else body
    bms = (root or {}).get("bookmakers") or {}
    for bk, b in bms.items():
        for mid, mk in ((b or {}).get("markets") or {}).items():
            for oid, oc in ((mk or {}).get("outcomes") or {}).items():
                for pid, snaps in ((oc or {}).get("players") or {}).items():
                    for sn in snaps or []:
                        ts = _iso(sn.get("createdAt") or sn.get("changedAt"))
                        pr = sn.get("price")
                        if ts is None or pr in (None, ""):
                            continue
                        out.append((fixture_id, bk, str(mid), str(oid), str(pid), ts, float(pr), 1 if sn.get("active", True) else 0))
    return out


class Study:
    """Builds the Pinnacle-vs-Kalshi dataset, spending OddsPapi requests only on (1) league/market metadata once,
    (2) fixture lists, (3) one historical-odds call per game - and only for games already matched to Kalshi."""

    def __init__(self, op: OddsPapi, wdb, kalshi_client, warehouse=None):
        self.op, self.db, self.k, self.wh = op, wdb, kalshi_client, warehouse
        self.db.conn.executescript(STUDY_SCHEMA)
        self.db.conn.commit()
        self.state = self.db.get("sp_study") or {}

    def log(self, msg):
        self.state.setdefault("log", []).append(f"{time.strftime('%H:%M:%S')} {msg}")
        self.state["log"] = self.state["log"][-60:]
        self.db.put("sp_study", self.state)

    # ---- metadata (cached forever) ----
    def meta(self, league) -> dict:
        md = self.db.get("sp_meta") or {}
        if league in md and md[league].get("tournament_id") and md[league].get("market_id"):
            return md[league]
        sport_slug, t_slug, _ = LEAGUES[league]
        sports = self.db.get("sp_sports") or {}
        if not sports:
            for s_ in self.op.get("/sports", note="meta:sports"):
                sports[str(s_.get("slug"))] = s_.get("sportId")
            self.db.put("sp_sports", sports)
        sid = sports.get(sport_slug)
        cur = md.get(league) or {"sport_id": sid}
        if not cur.get("tournament_id"):
            for t in self.op.get("/tournaments", {"sportId": sid}, note=f"meta:tournaments:{league}"):
                if str(t.get("tournamentSlug")).lower() == t_slug:
                    cur["tournament_id"] = t.get("tournamentId")
                    break
        if not cur.get("market_id"):
            mk = self.op.get("/markets", {"sportId": sid}, note=f"meta:markets:{league}")
            self.op.save_raw(f"markets:{league}", mk)
            cands = []
            for m in mk if isinstance(mk, list) else []:
                outs = m.get("outcomes") or []
                name = str(m.get("marketName") or "").lower()
                if len(outs) != 2 or m.get("playerProp") or m.get("handicap"):
                    continue
                per = str(m.get("period") or "").lower()
                score = (0 if ("moneyline" in name or "winner" in name or "1x2" in name) else 1,
                         0 if ("incl" in name or "overtime" in name or "extra" in name) else 1,
                         0 if per in ("", "result", "fulltime", "full time", "ft", "match", "game") else 1)
                cands.append((score, m))
            cands.sort(key=lambda x: x[0])
            cur["market_candidates"] = [{"id": str(m.get("marketId")), "name": m.get("marketName"), "period": m.get("period"),
                                         "outcomes": [o.get("outcomeName") for o in m.get("outcomes")]} for _, m in cands[:8]]
            if cands:
                m = cands[0][1]
                cur["market_id"] = str(m.get("marketId"))
                cur["market_name"] = m.get("marketName")
                cur["outcomes"] = [{"id": str(o.get("outcomeId")), "name": o.get("outcomeName")} for o in m.get("outcomes")]
        md[league] = cur
        self.db.put("sp_meta", md)
        return cur

    # ---- fixtures ----
    def fixtures(self, league, days_back: float) -> int:
        mt = self.meta(league)
        if not mt.get("tournament_id"):
            raise RuntimeError(f"{league}: tournament not found at OddsPapi")
        now = time.time()
        fmt = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        body = self.op.get("/fixtures", {"tournamentId": mt["tournament_id"], "from": fmt(now - days_back * 86400),
                                          "to": fmt(now), "statusId": 2}, note=f"fixtures:{league}")
        n = 0
        for f in body if isinstance(body, list) else []:
            st = _iso(f.get("startTime"))
            if not f.get("fixtureId") or not st:
                continue
            old = self.db.query("SELECT hist_pulled FROM sp_fixtures WHERE fixture_id=?", (f["fixtureId"],))
            if old:
                continue
            self.db.execute("INSERT INTO sp_fixtures(fixture_id, league, start_ts, status, p1, p2, p1_abbr, p2_abbr, raw) "
                            "VALUES(?,?,?,?,?,?,?,?,?)",
                            (f["fixtureId"], league, st, f.get("statusId"), f.get("participant1Name"), f.get("participant2Name"),
                             f.get("participant1Abbr"), f.get("participant2Abbr"), json.dumps(f)[:4000]))
            n += 1
        return n

    # ---- Kalshi side (free) ----
    def match(self, league) -> dict:
        series = LEAGUES[league][2]
        fx = self.db.query("SELECT * FROM sp_fixtures WHERE league=? AND kalshi_event IS NULL", (league,))
        if not fx:
            return {"matched": 0, "unmatched": 0}
        lo = min(f["start_ts"] for f in fx) - 2 * 86400
        hi = max(f["start_ts"] for f in fx) + 3 * 86400
        events: dict[str, list] = {}
        for status in ("settled", "closed"):
            for m in self.k.paginate("/markets", "markets", {"series_ticker": series, "status": status, "limit": 1000,
                                                             "min_close_ts": int(lo), "max_close_ts": int(hi)}, max_items=20000):
                events.setdefault(m.get("event_ticker"), [])
                if all(x["ticker"] != m["ticker"] for x in events[m["event_ticker"]]):
                    events[m["event_ticker"]].append(m)
        # games settled before Kalshi's historical cutoff only exist in the historical tier
        try:
            for m in self.k.paginate("/historical/markets", "markets", {"series_ticker": series, "limit": 1000}, max_items=20000):
                ct = _iso(m.get("close_time"))
                if ct and lo <= ct <= hi and m.get("event_ticker"):
                    events.setdefault(m["event_ticker"], [])
                    if all(x["ticker"] != m["ticker"] for x in events[m["event_ticker"]]):
                        events[m["event_ticker"]].append(m)
        except Exception:
            pass
        byt = {m["ticker"]: m for ms in events.values() for m in ms}
        ok = bad = 0
        try:
            s_ = self.k.series(series) or {"ticker": series}
            self.db.upsert_series(s_, "Sports")
        except Exception:
            pass
        for f in fx:
            r = match_fixture(f, events)
            if not r:
                bad += 1
                self.db.execute("UPDATE sp_fixtures SET match_note='no Kalshi match' WHERE fixture_id=?", (f["fixture_id"],))
                continue
            ev, t1, t2, note = r
            self.db.execute("UPDATE sp_fixtures SET kalshi_event=?, p1_market=?, p2_market=?, match_note=? WHERE fixture_id=?",
                            (ev, t1, t2, note, f["fixture_id"]))
            for t in (t1, t2):
                self.db.upsert_market(byt[t], series, "Sports", "live")
            ok += 1
        return {"matched": ok, "unmatched": bad, "kalshi_events_seen": len(events)}

    def sync_kalshi(self, fixture: dict, window_before_h=48, window_after_h=5):
        """1-minute candles + every trade from 48 h before the start to 5 h after, for both team markets."""
        start = fixture["start_ts"]
        a, b = int(start - window_before_h * 3600), int(min(time.time(), start + window_after_h * 3600))
        cut = self.wh.cutoff() if self.wh else {}
        series = LEAGUES[fixture["league"]][2]
        for t in (fixture["p1_market"], fixture["p2_market"]):
            row = self.db.query("SELECT tier, settled_ts FROM wh_markets WHERE ticker=?", (t,))
            hist_tr = cut.get("trades_created_ts")
            routes = []
            if hist_tr and a < hist_tr:
                routes.append(("/historical/trades", a, int(min(b, hist_tr))))
            if not hist_tr or b > hist_tr:
                routes.append(("/markets/trades", int(max(a, hist_tr or 0)), b))
            for path, x, y in routes:
                batch = []
                for tr in self.k.paginate(path, "trades", {"ticker": t, "min_ts": x, "max_ts": y, "limit": 1000}, max_items=300000):
                    batch.append(tr)
                    if len(batch) >= 2000:
                        self.db.add_trades(batch)
                        batch = []
                if batch:
                    self.db.add_trades(batch)
            hist = cut.get("market_settled_ts") and row and row[0]["settled_ts"] and row[0]["settled_ts"] < cut["market_settled_ts"]
            for x in range(a, b, 4 * 86400):             # candle requests in <= 4-day chunks
                y = min(b, x + 4 * 86400)
                path = f"/historical/markets/{t}/candlesticks" if hist else f"/series/{series}/markets/{t}/candlesticks"
                try:
                    r = self.k.request("GET", path, {"start_ts": x, "end_ts": y, "period_interval": 1})
                except Exception:
                    path = f"/historical/markets/{t}/candlesticks"
                    r = self.k.request("GET", path, {"start_ts": x, "end_ts": y, "period_interval": 1})
                self.db.add_candles(t, 1, r.get("candlesticks") or [])
            self.db.execute("UPDATE wh_markets SET complete=1, trades_synced_to=?, candles_synced_to=? WHERE ticker=?", (b, b, t))
        self.db.execute("UPDATE sp_fixtures SET kalshi_synced=? WHERE fixture_id=?", (time.time(), fixture["fixture_id"]))

    # ---- Pinnacle history (1 request per game) ----
    def pull_history(self, fixture: dict) -> int:
        body = self.op.get("/historical-odds", {"fixtureId": fixture["fixture_id"], "bookmakers": "pinnacle,kalshi,polymarket"},
                           note=f"hist:{fixture['league']}")
        if not self.db.get("sp_raw_hist_sample"):
            self.op.save_raw("hist_sample", body)
            self.db.put("sp_raw_hist_sample", fixture["fixture_id"])
        rows = walk_historical(body, fixture["fixture_id"])
        if rows:
            self.db.executemany("INSERT OR REPLACE INTO sp_odds VALUES(?,?,?,?,?,?,?,?)", rows)
        self.db.execute("UPDATE sp_fixtures SET hist_pulled=?, hist_rows=? WHERE fixture_id=?",
                        (time.time(), len(rows), fixture["fixture_id"]))
        return len(rows)

    def run(self, leagues=("NFL", "NBA"), days_back=21, max_games=20, refresh_fixtures=True) -> dict:
        self.state = {"running": True, "started": time.time(), "log": [], "leagues": list(leagues), "max_games": max_games}
        self.db.put("sp_study", self.state)
        try:
            for lg in leagues:
                if refresh_fixtures:
                    self.log(f"{lg}: fixtures +{self.fixtures(lg, days_back)}")
                self.log(f"{lg}: Kalshi match {self.match(lg)}")
            todo = self.db.query(f"SELECT * FROM sp_fixtures WHERE kalshi_event IS NOT NULL AND hist_pulled IS NULL "
                                 f"AND league IN ({','.join('?' * len(leagues))}) ORDER BY start_ts DESC", list(leagues))
            # alternate leagues so a small batch covers both
            by = {lg: [f for f in todo if f["league"] == lg] for lg in leagues}
            order = []
            while any(by.values()) and len(order) < max_games:
                for lg in leagues:
                    if by[lg] and len(order) < max_games:
                        order.append(by[lg].pop(0))
            for i, f in enumerate(order):
                if self.op.usage()["left"] <= 0:
                    self.log("OddsPapi budget exhausted - stopping")
                    break
                n = self.pull_history(f)
                self.sync_kalshi(f)
                self.log(f"{i + 1}/{len(order)} {f['league']} {f['p1']} vs {f['p2']}: {n} odds rows, Kalshi synced")
        except Exception as e:
            self.log(f"ERROR {type(e).__name__}: {str(e)[:300]}")
        finally:
            self.state["running"] = False
            self.state["finished"] = time.time()
            self.state["usage"] = self.op.usage()
            self.db.put("sp_study", self.state)
        return self.state

    def summary(self) -> dict:
        q = lambda s, a=(): self.db.query(s, a)
        return {"fixtures": q("SELECT league, count(*) n, sum(kalshi_event IS NOT NULL) matched, sum(hist_pulled IS NOT NULL) pulled, "
                             "sum(COALESCE(hist_rows,0)) odds_rows, sum(kalshi_synced IS NOT NULL) synced FROM sp_fixtures GROUP BY league"),
                "unmatched_examples": q("SELECT league, p1, p2, p1_abbr, p2_abbr, start_ts FROM sp_fixtures WHERE match_note='no Kalshi match' LIMIT 8"),
                "odds_by_book": q("SELECT bookmaker, market_id, count(*) n, count(DISTINCT fixture_id) games FROM sp_odds GROUP BY bookmaker, market_id ORDER BY n DESC LIMIT 20"),
                "meta": self.db.get("sp_meta"), "state": self.db.get("sp_study")}


# ================================================================== model ==
class SportsModel:
    """Pinnacle no-vig fair value for a Kalshi team market at time t, using only odds seen by t - lag."""

    def __init__(self, wdb, lag_sec: float = 60.0):
        self.db = wdb
        self.db.conn.executescript(STUDY_SCHEMA)
        self.lag = lag_sec
        self.meta = wdb.get("sp_meta") or {}
        self.by_market: dict[str, tuple] = {}
        for f in wdb.query("SELECT * FROM sp_fixtures WHERE kalshi_event IS NOT NULL AND hist_pulled IS NOT NULL"):
            self.by_market[f["p1_market"]] = (f, 0)
            self.by_market[f["p2_market"]] = (f, 1)
        self._series: dict[str, tuple] = {}

    def series(self, fixture_id, league):
        if fixture_id not in self._series:
            mid = (self.meta.get(league) or {}).get("market_id")
            outs = [o["id"] for o in (self.meta.get(league) or {}).get("outcomes") or []]
            rows = self.db.query("SELECT outcome_id, ts, price, active FROM sp_odds WHERE fixture_id=? AND bookmaker='pinnacle' "
                                 "AND market_id=? ORDER BY ts", (fixture_id, mid))
            # outcome order: as listed by /markets (first = participant 1)
            oid = outs if len(outs) == 2 else sorted({r["outcome_id"] for r in rows})[:2]
            ser = {o: ([], []) for o in oid}
            for r in rows:
                if r["outcome_id"] in ser:
                    ser[r["outcome_id"]][0].append(r["ts"])
                    ser[r["outcome_id"]][1].append((r["price"], r["active"]))
            self._series[fixture_id] = (oid, ser)
        return self._series[fixture_id]

    def pinnacle_p(self, fixture, t) -> float | None:
        import bisect
        oid, ser = self.series(fixture["fixture_id"], fixture["league"])
        if len(oid) != 2:
            return None
        inv = []
        for o in oid:
            ts, pv = ser[o]
            i = bisect.bisect_right(ts, t - self.lag)
            if not i:
                return None
            price, active = pv[i - 1]
            if not active or price <= 1.0:
                return None
            inv.append(1 / price)
        return inv[0] / (inv[0] + inv[1])

    def fair(self, market: dict, t: float) -> dict | None:
        x = self.by_market.get(market.get("ticker"))
        if not x:
            return None
        f, side = x
        p1 = self.pinnacle_p(f, t)
        if p1 is None:
            return None
        return {"p_yes": p1 if side == 0 else 1 - p1, "start_ts": f["start_ts"], "minutes_to_start": (f["start_ts"] - t) / 60,
                "league": f["league"], "fixture": f["fixture_id"]}


def evaluate(wdb, leads_min=(1440, 360, 120, 30, 5), lag_sec=60.0) -> dict:
    """Scorecard + out-of-sample blend test: Kalshi mid (1-min candle) vs Pinnacle no-vig at fixed minutes before
    the game, against the actual Kalshi result. Pregame only."""
    import math
    model = SportsModel(wdb, lag_sec)
    rows = []
    for tk, (f, side) in model.by_market.items():
        res = wdb.query("SELECT result FROM wh_markets WHERE ticker=?", (tk,))
        if not res or res[0]["result"] not in ("yes", "no"):
            continue
        y = 1.0 if res[0]["result"] == "yes" else 0.0
        for L in leads_min:
            t = f["start_ts"] - L * 60
            fv = model.fair({"ticker": tk}, t)
            b = wdb.query("SELECT bid_c, ask_c, end_ts FROM wh_candles WHERE ticker=? AND period=1 AND end_ts<=? AND end_ts>=? "
                          "ORDER BY end_ts DESC LIMIT 1", (tk, t, t - 30 * 60))
            if not fv or not b or b[0]["bid_c"] is None or b[0]["ask_c"] is None or not 0 < b[0]["bid_c"] < b[0]["ask_c"] < 1:
                continue
            mid = (b[0]["bid_c"] + b[0]["ask_c"]) / 2
            rows.append({"lead": L, "day": dt.datetime.fromtimestamp(f["start_ts"], dt.timezone.utc).date().isoformat(), "y": y,
                         "m": mid, "p": fv["p_yes"], "bid": b[0]["bid_c"], "ask": b[0]["ask_c"], "league": f["league"]})
    cl = lambda q: min(1 - 1e-3, max(1e-3, q))
    ll = lambda P, Y: -sum(math.log(cl(p)) if y else math.log(1 - cl(p)) for p, y in zip(P, Y)) / len(Y)
    br = lambda P, Y: sum((p - y) ** 2 for p, y in zip(P, Y)) / len(Y)
    out = {"rows": len(rows), "games": len({r["day"] + str(r["y"]) for r in rows}), "by_lead": {}}
    for L in leads_min:
        R = [r for r in rows if r["lead"] == L]
        if len(R) < 2:
            out["by_lead"][str(L)] = {"n": len(R)}
            continue
        Y = [r["y"] for r in R]
        gap = [r["p"] - r["m"] for r in R]
        # taker test at the quote: buy YES if ask < fair - fee - 1c, buy NO if bid > fair + fee + 1c
        pnl = n = 0.0
        for r in R:
            fee_y = 0.07 * r["ask"] * (1 - r["ask"])
            fee_n = 0.07 * r["bid"] * (1 - r["bid"])
            if r["ask"] + fee_y + 0.01 <= r["p"]:
                pnl += r["y"] - r["ask"] - fee_y
                n += 1
            elif r["bid"] - fee_n - 0.01 >= r["p"]:
                pnl += (1 - r["y"]) - (1 - r["bid"]) - fee_n
                n += 1
        out["by_lead"][str(L)] = {
            "n": len(R), "logloss_kalshi": round(ll([r["m"] for r in R], Y), 4), "logloss_pinnacle": round(ll([r["p"] for r in R], Y), 4),
            "brier_kalshi": round(br([r["m"] for r in R], Y), 5), "brier_pinnacle": round(br([r["p"] for r in R], Y), 5),
            "mean_abs_gap_c": round(100 * sum(abs(g) for g in gap) / len(gap), 2),
            "agreement_check": round(sum((r["p"] - .5) * (r["m"] - .5) for r in R) / len(R) * 100, 3),
            "taker_trades": int(n), "taker_pnl_per_trade_c": round(100 * pnl / n, 2) if n else None}
    wdb.put("sp_eval", {**out, "ts": time.time()})
    return out
