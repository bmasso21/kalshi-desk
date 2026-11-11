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

ODDSPAPI = "https://v5.oddspapi.io/en"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sp_calls (ts REAL, path TEXT, params TEXT, status INTEGER, bytes INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS sp_raw (k TEXT PRIMARY KEY, ts REAL, body TEXT);
"""


class BudgetExceeded(Exception):
    pass


class OddsPapi:
    def __init__(self, cfg_getter, wdb, session=None, base=ODDSPAPI):
        self._cfg = cfg_getter          # callable -> the "sports" config section (read fresh each call)
        self.db = wdb
        self.db.conn.executescript(SCHEMA)
        self.db.conn.commit()
        self.http = session or requests.Session()
        self.base = base
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
    def configured(self) -> bool:
        return bool((self._cfg().get("oddspapi_key") or "").strip())

    def get(self, path: str, params: dict | None = None, note: str = "") -> object:
        key = (self._cfg().get("oddspapi_key") or "").strip()
        if not key:
            raise RuntimeError("No OddsPapi key yet - add it in Settings")
        self._spend(1)                          # counted before sending: a failed call may still count upstream
        params = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            r = self.http.get(self.base + path, params=params, headers={"X-API-Key": key}, timeout=60)
        except Exception as e:
            self.db.execute("INSERT INTO sp_calls VALUES(?,?,?,?,?,?)", (time.time(), path, json.dumps(params), -1, 0, f"{note} {e}"[:300]))
            raise
        self.db.execute("INSERT INTO sp_calls VALUES(?,?,?,?,?,?)",
                        (time.time(), path, json.dumps(params), r.status_code, len(r.content), note[:200]))
        if r.status_code >= 400:
            body = r.text[:300].replace(key, "***")
            raise RuntimeError(f"OddsPapi {path} HTTP {r.status_code}: {body}")
        return r.json()

    def calls(self, limit=100) -> list[dict]:
        return self.db.query("SELECT * FROM sp_calls ORDER BY ts DESC LIMIT ?", (limit,))

    def save_raw(self, k, body):
        self.db.execute("INSERT OR REPLACE INTO sp_raw VALUES(?,?,?)", (k, time.time(), json.dumps(body)))

    def raw(self, k):
        r = self.db.query("SELECT ts, body FROM sp_raw WHERE k=?", (k,))
        return {"ts": r[0]["ts"], "body": json.loads(r[0]["body"])} if r else None


SPORT_HINTS = {"basketball": "NBA", "american-football": "NFL", "ice-hockey": "NHL", "baseball": "MLB"}


def discover(op: OddsPapi, kalshi_client=None, max_requests: int = 14) -> dict:
    """Step 1 (≈12 requests): sports, the NBA/NFL/NHL/MLB tournament ids, Pinnacle + Kalshi bookmaker metadata,
    moneyline market ids, this week's NFL fixtures and their Kalshi mapping. Raw responses are stored for review.
    Kalshi side (free, public): which game series exist."""
    out = {"started": time.time(), "steps": [], "kalshi": {}}
    start_used = op.usage()["used"]

    def step(name, path, params=None):
        if op.usage()["used"] - start_used >= max_requests:
            out["steps"].append({"step": name, "skipped": "request limit for this run"})
            return None
        try:
            body = op.get(path, params, note=f"discover:{name}")
            op.save_raw(f"discover:{name}", body)
            n = len(body) if isinstance(body, list) else (len(body.get("data", [])) if isinstance(body, dict) and isinstance(body.get("data"), list) else None)
            out["steps"].append({"step": name, "ok": True, "items": n})
            return body
        except Exception as e:
            out["steps"].append({"step": name, "ok": False, "error": str(e)[:300]})
            return None

    sports = step("sports", "/sports") or []
    rows = sports if isinstance(sports, list) else sports.get("data", []) if isinstance(sports, dict) else []
    ids = {}
    for s in rows:
        slug = str(s.get("slug") or "").lower()
        for k in SPORT_HINTS:
            if slug == k:
                ids[k] = s.get("sportId")
    out["sport_ids"] = ids
    step("bookmakers", "/bookmakers", {"bookmakers": "pinnacle,kalshi,polymarket"})
    tourn = {}
    for slug, sid in ids.items():
        body = step(f"tournaments:{slug}", "/tournaments", {"sportId": sid}) or []
        lst = body if isinstance(body, list) else body.get("data", []) if isinstance(body, dict) else []
        want = SPORT_HINTS[slug]
        for t in lst:
            if str(t.get("tournamentName") or "").upper() == want or str(t.get("tournamentSlug") or "").lower() == want.lower():
                tourn[want] = t.get("tournamentId")
    out["tournament_ids"] = tourn
    for slug, sid in ids.items():
        step(f"markets:{slug}", "/markets", {"sportId": sid})
    if tourn.get("NFL"):
        now = int(time.time())
        fx = step("fixtures:NFL", "/fixtures", {"tournamentId": tourn["NFL"], "startTimeFrom": now - 8 * 86400,
                                                 "startTimeTo": now + 6 * 86400}) or []
        lst = fx if isinstance(fx, list) else fx.get("data", []) if isinstance(fx, dict) else []
        fids = [f.get("fixtureId") for f in lst if f.get("fixtureId")][:40]
        if fids:
            step("mapping:NFL:kalshi", "/fixtures/mapping", {"bookmaker": "kalshi", "fixtureIds": ",".join(fids)})
    # Kalshi side (public, free): game-winner series
    if kalshi_client is not None:
        try:
            ser = kalshi_client.series_list(category="Sports")
            cand = [s for s in ser if any(x in (s.get("ticker") or "") for x in ("NFLGAME", "NBAGAME", "NHLGAME", "MLBGAME"))]
            out["kalshi"] = {"sports_series": len(ser),
                             "game_series": [{k: s.get(k) for k in ("ticker", "title", "fee_type", "fee_multiplier", "tags")}
                                             for s in cand][:20]}
        except Exception as e:
            out["kalshi"] = {"error": str(e)[:300]}
    out["finished"] = time.time()
    out["usage"] = op.usage()
    op.db.put("sp_discover", out)
    return out
