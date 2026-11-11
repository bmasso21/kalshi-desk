"""Sports fair value research (Phase 2.2): Pinnacle line history from OddsPapi (free tier) vs Kalshi game markets.

OddsPapi free plan = 250 requests / month, so every call goes through a hard budget:
  - counted locally per calendar month (UTC) BEFORE the request is sent, persisted in the warehouse
  - refused once `monthly_cap` is reached (default 240, leaving a little headroom)
  - every call is logged (path + params, never the key)
The key lives only in config.json (entered by Brian in Settings), is sent as the X-API-Key header, and is never
returned to the browser, logged, or written to the audit trail.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import time

import requests

ODDSPAPI = "https://api.oddspapi.io/v4"     # free-signup keys are for v4 (v5 rejects them)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sp_calls (ts REAL, path TEXT, params TEXT, status INTEGER, bytes INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS sp_raw (k TEXT PRIMARY KEY, ts REAL, body TEXT);
"""


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
        self._spend(1)                          # counted before sending: a failed call may still count upstream
        params = {k: v for k, v in (params or {}).items() if v is not None}
        mode = self._cfg().get("auth_mode", "query")          # v4 documents the apiKey query parameter
        q = dict(params, apiKey=key) if mode == "query" else params
        hdr = {"X-API-Key": key} if mode == "header" else {}
        red = lambda t: str(t).replace(key, "***")
        try:
            r = self.http.get(self.base + path, params=q, headers=hdr, timeout=60)
        except Exception as e:
            self.db.execute("INSERT INTO sp_calls VALUES(?,?,?,?,?,?)", (time.time(), path, json.dumps(params), -1, 0, red(f"{note} {e}")[:300]))
            raise RuntimeError(red(f"OddsPapi {path}: {type(e).__name__}: {e}")) from None
        self.db.execute("INSERT INTO sp_calls VALUES(?,?,?,?,?,?)",
                        (time.time(), path, json.dumps(params), r.status_code, len(r.content), note[:200]))
        if r.status_code >= 400:
            raise RuntimeError(red(f"OddsPapi {path} HTTP {r.status_code}: {r.text[:300]}"))
        return r.json()

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
