"""Weather fair values (Phase 2.1).

What the daily temperature markets settle on
  Each market's rules name a NWS climate-report station, e.g. "maximum temperature recorded at Chicago (CLIMDW)".
  The value is the NWS Daily Climate Report (CLI) high/low for that station, for the calendar day in LOCAL
  STANDARD TIME (during daylight time the day runs 1 AM - 1 AM local). Whole degrees F.

Inputs (all free, all with history so the model can be backtested exactly as it runs live)
  forecast   NWS National Blend of Models station guidance (NBS): 3-hourly temperature + 12-h max/min,
             runs every 6 h. Archive + realtime from the Iowa Environmental Mesonet (IEM).
  observed   ASOS observations for the same station (IEM): running high/low so far today.
  truth      CLI reports (IEM json/cli.py) - only used to calibrate, never ahead of time.

Model, for station S, day D, decision time t (nothing after t is ever used)
  W      = rest of the climate day: [max(t, D start), D end)
  M      = observed extreme so far today (obs up to t - obs_lag)
  x      = forecast extreme over W from the latest NBS run published by t (runtime + run_lag <= t);
           if W contains no forecast hour, x = latest observation
  Y      = extreme over W  ~  x + residual, residuals = empirical (y - x) from PRIOR days at the same
           hours-before-day-end, pooled over stations (+ shrunk per-station bias), kernel-smoothed
  O      = max(M, Y) for highs, min(M, Y) for lows (rounded to whole degrees, i.e. the obs-based day extreme)
  T      = O + c, c = empirical (CLI - obs-based day extreme) for S from prior days (5-min ASOS can miss
           the true peak by a degree; this learns that gap)
  P(bucket) = sum of P(T = k) over the bucket's whole degrees.
Calibration only uses days whose CLI had been published before t (day end + 12 h).
"""
from __future__ import annotations

import bisect
import csv
import datetime as dt
import io
import json
import math
import re
import threading
import time
import traceback

import requests

IEM = "https://mesonet.agron.iastate.edu"
UA = {"User-Agent": "KalshiDesk/2.1 (personal research)"}

# CLI code (from the market rules) -> (ICAO for NBS/CLI, IEM ASOS id, standard-time UTC offset in hours)
STATIONS = {
    "MDW": ("KMDW", "MDW", -6), "ORD": ("KORD", "ORD", -6), "NYC": ("KNYC", "NYC", -5), "LGA": ("KLGA", "LGA", -5),
    "JFK": ("KJFK", "JFK", -5), "ATL": ("KATL", "ATL", -5), "AUS": ("KAUS", "AUS", -6), "DEN": ("KDEN", "DEN", -7),
    "LAX": ("KLAX", "LAX", -8), "MIA": ("KMIA", "MIA", -5), "PHL": ("KPHL", "PHL", -5), "BOS": ("KBOS", "BOS", -5),
    "DFW": ("KDFW", "DFW", -6), "DAL": ("KDAL", "DAL", -6), "DCA": ("KDCA", "DCA", -5), "IAD": ("KIAD", "IAD", -5),
    "EWR": ("KEWR", "EWR", -5), "HOU": ("KHOU", "HOU", -6), "IAH": ("KIAH", "IAH", -6), "LAS": ("KLAS", "LAS", -8),
    "MSP": ("KMSP", "MSP", -6), "MSY": ("KMSY", "MSY", -6), "OKC": ("KOKC", "OKC", -6), "PHX": ("KPHX", "PHX", -7),
    "SAN": ("KSAN", "SAN", -8), "SAT": ("KSAT", "SAT", -6), "SDF": ("KSDF", "SDF", -5), "SEA": ("KSEA", "SEA", -8),
    "SFO": ("KSFO", "SFO", -8), "TTN": ("KTTN", "TTN", -5), "DTW": ("KDTW", "DTW", -5), "CLT": ("KCLT", "CLT", -5),
    "BNA": ("KBNA", "BNA", -6), "SLC": ("KSLC", "SLC", -7), "PDX": ("KPDX", "PDX", -8), "TPA": ("KTPA", "TPA", -5),
}

RULE_RE = re.compile(r"(maximum|minimum) temperature recorded at (.+?) \((CLI[A-Z0-9]{3})\)", re.I)
EVENT_DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})$")
MONTHS = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}

# hours before the end of the climate day -> calibration bucket (upper bounds) and the sample point used per bucket
BUCKETS = [(3, 1.5), (6, 4.5), (12, 9.0), (18, 15.0), (24, 21.0), (36, 30.0), (60, 48.0)]


def bucket_of(hours_left: float) -> int | None:
    for i, (hi, _) in enumerate(BUCKETS):
        if hours_left <= hi:
            return i
    return None


def parse_rules(rules: str | None) -> tuple[str, str] | None:
    """-> ('high'|'low', 'MDW') from a market's rules_primary."""
    m = RULE_RE.search(rules or "")
    if not m:
        return None
    return ("high" if m.group(1).lower() == "maximum" else "low"), m.group(3).upper()[3:]


def event_date(event_ticker: str) -> dt.date | None:
    m = EVENT_DATE_RE.search(event_ticker or "")
    if not m or m.group(2) not in MONTHS:
        return None
    try:
        return dt.date(2000 + int(m.group(1)), MONTHS[m.group(2)], int(m.group(3)))
    except ValueError:
        return None


def day_window(d: dt.date, std_offset_h: float) -> tuple[float, float]:
    """UTC [start, end) of climate day d at a station whose standard time is UTC+std_offset_h."""
    start = dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp() - std_offset_h * 3600
    return start, start + 86400


def bucket_yes_prob(dist: dict[int, float], strike_type: str, floor, cap) -> float | None:
    """P(YES) for a Kalshi temperature bucket given P(T = k)."""
    if not dist:
        return None
    if strike_type == "greater" and floor is not None:
        return sum(p for k, p in dist.items() if k > floor)
    if strike_type == "less" and cap is not None:
        return sum(p for k, p in dist.items() if k < cap)
    if strike_type == "between" and floor is not None and cap is not None:
        return sum(p for k, p in dist.items() if floor <= k <= cap)
    return None


def _phi(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _num(x):
    if x in (None, "", "M", "T", "None"):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _ts(s: str) -> int | None:
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%MZ"):
        try:
            return int(dt.datetime.strptime(s, fmt).replace(tzinfo=dt.timezone.utc).timestamp())
        except ValueError:
            continue
    return None


SCHEMA = """
CREATE TABLE IF NOT EXISTS wx_nbs (station TEXT, runtime INTEGER, ftime INTEGER, tmp REAL, txn REAL,
    PRIMARY KEY (station, runtime, ftime));
CREATE TABLE IF NOT EXISTS wx_obs (station TEXT, ts INTEGER, tmpf REAL, PRIMARY KEY (station, ts));
CREATE TABLE IF NOT EXISTS wx_cli (station TEXT, date TEXT, high REAL, low REAL, product TEXT,
    PRIMARY KEY (station, date));
CREATE TABLE IF NOT EXISTS wx_runs (runtime INTEGER PRIMARY KEY, first_seen REAL, stations INTEGER);
"""


# ============================================================== data access ==
class WxStore:
    def __init__(self, wdb):
        self.db = wdb
        self.db.conn.executescript(SCHEMA)
        self.db.conn.commit()

    # ---- series / stations in the warehouse ----
    def series_map(self) -> dict[str, dict]:
        """series_ticker -> {kind, cli, icao, asos, offset, city} for every temperature series we can price."""
        out = {}
        series = [r["series_ticker"] for r in self.db.query(
            "SELECT DISTINCT series_ticker FROM wh_markets WHERE series_ticker LIKE 'KX%' "
            "AND raw LIKE '%temperature recorded at%'")]
        for st in series:
            # rule wording changed over time: use the newest market whose rules name a climate-report station
            for r in self.db.query("SELECT raw FROM wh_markets WHERE series_ticker=? AND raw LIKE '%temperature recorded at%' "
                                   "ORDER BY close_ts DESC LIMIT 40", (st,)):
                try:
                    raw = json.loads(r["raw"] or "{}")
                except ValueError:
                    continue
                p = parse_rules(raw.get("rules_primary"))
                if not p:
                    continue
                kind, cli = p
                stn = STATIONS.get(cli)
                out[st] = {"kind": kind, "cli": cli, "icao": stn[0] if stn else None,
                           "asos": stn[1] if stn else None, "offset": stn[2] if stn else None,
                           "city": (raw.get("custom_strike") or {}).get("City"), "mapped": bool(stn)}
                break
        return out

    def stations(self) -> list[str]:
        return sorted({v["cli"] for v in self.series_map().values() if v["mapped"]})

    # ---- writes ----
    def add_nbs(self, rows) -> int:
        return self.db.executemany("INSERT OR REPLACE INTO wx_nbs VALUES(?,?,?,?,?)", rows)

    def add_obs(self, rows) -> int:
        return self.db.executemany("INSERT OR REPLACE INTO wx_obs VALUES(?,?,?)", rows)

    def add_cli(self, rows) -> int:
        return self.db.executemany("INSERT OR REPLACE INTO wx_cli VALUES(?,?,?,?,?)", rows)

    def counts(self) -> dict:
        q = lambda s: self.db.query(s)[0]
        return {"nbs_rows": q("SELECT count(*) n FROM wx_nbs")["n"],
                "nbs_runs": q("SELECT count(DISTINCT runtime) n FROM wx_nbs")["n"],
                "nbs_last_run": q("SELECT max(runtime) n FROM wx_nbs")["n"],
                "obs_rows": q("SELECT count(*) n FROM wx_obs")["n"],
                "obs_last": q("SELECT max(ts) n FROM wx_obs")["n"],
                "cli_days": q("SELECT count(*) n FROM wx_cli")["n"],
                "cli_last": q("SELECT max(date) n FROM wx_cli")["n"]}


# =============================================================== collector ==
class WxCollector:
    """Backfills and keeps current: NBS runs, ASOS obs and CLI reports for every station the temperature
    markets settle on. Public data only; no Kalshi key involved."""

    def __init__(self, engine, store: WxStore, session=None):
        self.e = engine
        self.store = store
        self.http = session or requests.Session()
        self.http.headers.update(UA)
        self._stop = threading.Event()
        self._now = threading.Event()
        self.state = {"running": False, "phase": "idle", "progress": "", "error": None, "requests": 0,
                      "last_sync": store.db.get("wx_last_sync"), "unmapped": [], "errors": 0}

    @property
    def cfg(self) -> dict:
        return (self.e.cfg.data.get("weather") or {}) if self.e else {}

    def _get(self, path, params, timeout=60):
        self.state["requests"] += 1
        time.sleep(float(self.cfg.get("request_gap_sec", 0.4)))
        r = self.http.get(IEM + path, params=params, timeout=timeout)
        return r

    # ---- NBS ----
    def fetch_nbs(self, icaos: list[str], runtime: int | None) -> list[tuple]:
        params = [("station", s) for s in icaos] + [("model", "NBS")]
        if runtime is not None:
            params.append(("runtime", dt.datetime.fromtimestamp(runtime, dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")))
        r = self._get("/api/1/mos.txt", params)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return self.parse_nbs(r.text)

    @staticmethod
    def parse_nbs(text: str, horizon_h: float = 96) -> list[tuple]:
        out = []
        for row in csv.DictReader(io.StringIO(text)):
            rt, ft = _ts(row.get("runtime")), _ts(row.get("ftime"))
            if rt is None or ft is None or ft - rt > horizon_h * 3600:
                continue
            tmp, txn = _num(row.get("tmp")), _num(row.get("txn"))
            if tmp is None and txn is None:
                continue
            out.append((row.get("station"), rt, ft, tmp, txn))
        return out

    @staticmethod
    def runtimes(start: float, end: float) -> list[int]:
        hours_new, hours_old = (0, 6, 12, 18), (1, 7, 13, 19)
        switch = dt.datetime(2026, 5, 5, tzinfo=dt.timezone.utc).timestamp()
        d = dt.datetime.fromtimestamp(start, dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        out = []
        while d.timestamp() <= end:
            for h in (hours_new if d.timestamp() >= switch else hours_old):
                t = d.timestamp() + h * 3600
                if start <= t <= end:
                    out.append(int(t))
            d += dt.timedelta(days=1)
        return out

    # ---- obs ----
    def fetch_obs(self, asos_id: str, start: float, end: float) -> list[tuple]:
        a = dt.datetime.fromtimestamp(start, dt.timezone.utc)
        b = dt.datetime.fromtimestamp(end, dt.timezone.utc)
        params = [("station", asos_id), ("data", "tmpf"), ("tz", "Etc/UTC"), ("format", "onlycomma"),
                  ("latlon", "no"), ("missing", "M"), ("trace", "T"), ("direct", "no"),
                  ("report_type", "1"), ("report_type", "3"), ("report_type", "4"),
                  ("year1", a.year), ("month1", a.month), ("day1", a.day), ("hour1", a.hour),
                  ("year2", b.year), ("month2", b.month), ("day2", b.day), ("hour2", b.hour)]
        r = self._get("/cgi-bin/request/asos.py", params, timeout=180)
        r.raise_for_status()
        return self.parse_obs(r.text, asos_id)

    @staticmethod
    def parse_obs(text: str, asos_id: str) -> list[tuple]:
        out = []
        for row in csv.DictReader(io.StringIO(text)):
            ts, t = _ts(row.get("valid")), _num(row.get("tmpf"))
            if ts is not None and t is not None and -80 < t < 140:
                out.append((asos_id, ts, t))
        return out

    # ---- CLI ----
    def fetch_cli(self, icao: str, year: int) -> list[tuple]:
        r = self._get("/json/cli.py", {"station": icao, "year": year})
        r.raise_for_status()
        return self.parse_cli(r.json(), icao)

    @staticmethod
    def parse_cli(js: dict, icao: str) -> list[tuple]:
        out = []
        for x in js.get("results") or []:
            hi, lo = _num(x.get("high")), _num(x.get("low"))
            if x.get("valid") and (hi is not None or lo is not None):
                out.append((icao, x["valid"], hi, lo, x.get("product")))
        return out

    # ---- sync ----
    def sync(self, now: float | None = None) -> dict:
        now = now or time.time()
        stats = {"nbs_rows": 0, "obs_rows": 0, "cli_rows": 0, "errors": 0}
        self.state.update(running=True, error=None)
        try:
            smap = self.store.series_map()
            self.state["unmapped"] = sorted({v["cli"] for v in smap.values() if not v["mapped"]})
            clis = sorted({v["cli"] for v in smap.values() if v["mapped"]})
            if not clis:
                return stats
            icaos = [STATIONS[c][0] for c in clis]
            first = self.store.db.query("SELECT min(close_ts) t FROM wh_markets WHERE series_ticker LIKE 'KX%' "
                                        "AND raw LIKE '%temperature recorded at%'")[0]["t"] or now
            start = max(first - 4 * 86400, now - float(self.cfg.get("lookback_days", 130)) * 86400)
            # 1) NBS runs not yet stored (or stored for fewer stations than we now track)
            self.state["phase"] = "forecasts"
            have = {r["runtime"]: r["n"] for r in self.store.db.query(
                "SELECT runtime, count(DISTINCT station) n FROM wx_nbs WHERE runtime >= ? GROUP BY runtime", (start,))}
            tr = self.store.db.get("wx_nbs_tried") or {}
            if not isinstance(tr, dict) or tr.get("stations") != icaos:
                tr = {"stations": icaos, "runs": []}          # station list changed -> retry every run
            tried = set(tr["runs"])
            todo = [rt for rt in self.runtimes(start, now - 3600)
                    if have.get(rt, 0) < len(icaos) and (rt not in tried or rt > now - 12 * 3600)]
            for i, rt in enumerate(todo):
                if self._stop.is_set():
                    break
                self.state["progress"] = f"forecast runs {i + 1}/{len(todo)}"
                for k in range(0, len(icaos), 6):
                    try:
                        rows = self.fetch_nbs(icaos[k:k + 6], rt)
                        stats["nbs_rows"] += self.store.add_nbs(rows)
                    except Exception as e:
                        stats["errors"] += 1
                        self.state["error"] = f"nbs {rt}: {e}"
                tried.add(rt)
                if rt < now - 12 * 3600 and i % 20 == 0:
                    self.store.db.put("wx_nbs_tried", {"stations": icaos, "runs": sorted(tried)[-2000:]})
            self.store.db.put("wx_nbs_tried", {"stations": icaos, "runs": sorted(tried)[-2000:]})
            # latest run (live): record when each run first appeared, to verify the backtest's publish lag
            for k in range(0, len(icaos), 6):
                try:
                    rows = self.fetch_nbs(icaos[k:k + 6], None)
                    stats["nbs_rows"] += self.store.add_nbs(rows)
                    for rt in {r[1] for r in rows}:
                        self.store.db.execute("INSERT OR IGNORE INTO wx_runs VALUES(?,?,?)", (rt, now, len(rows)))
                except Exception as e:
                    stats["errors"] += 1
                    self.state["error"] = f"nbs latest: {e}"
            # 2) observations: backfill in 20-day chunks, then keep the last 36 h fresh
            self.state["phase"] = "observations"
            for j, c in enumerate(clis):
                if self._stop.is_set():
                    break
                aid = STATIONS[c][1]
                last = self.store.db.query("SELECT max(ts) t FROM wx_obs WHERE station=?", (aid,))[0]["t"]
                a = max(start, (last or start) - 36 * 3600)
                while a < now and not self._stop.is_set():
                    b = min(now + 3600, a + 20 * 86400)
                    self.state["progress"] = f"obs {j + 1}/{len(clis)} {c}"
                    try:
                        stats["obs_rows"] += self.store.add_obs(self.fetch_obs(aid, a, b))
                    except Exception as e:
                        stats["errors"] += 1
                        self.state["error"] = f"obs {c}: {e}"
                    a = b
            # 3) climate reports (this year and, early in the year, last year)
            self.state["phase"] = "climate reports"
            years = sorted({dt.datetime.fromtimestamp(start, dt.timezone.utc).year,
                            dt.datetime.fromtimestamp(now, dt.timezone.utc).year})
            for j, c in enumerate(clis):
                if self._stop.is_set():
                    break
                self.state["progress"] = f"CLI {j + 1}/{len(clis)} {c}"
                for y in years:
                    try:
                        stats["cli_rows"] += self.store.add_cli(self.fetch_cli(STATIONS[c][0], y))
                    except Exception as e:
                        stats["errors"] += 1
                        self.state["error"] = f"cli {c}: {e}"
            self.state["last_sync"] = time.time()
            self.store.db.put("wx_last_sync", self.state["last_sync"])
            self.store.db.put("wx_last_stats", stats)
        except Exception as e:
            self.state["error"] = f"{type(e).__name__}: {e}"
            if self.e:
                self.e.db.audit("weather_error", traceback.format_exc()[-800:])
        finally:
            self.state["errors"] = stats["errors"]
            self.state.update(running=False, phase="idle", progress="")
        return stats

    def status(self) -> dict:
        return {**self.state, "counts": self.store.counts(), "last_stats": self.store.db.get("wx_last_stats")}

    def trigger(self):
        self._now.set()

    def loop(self):
        self._now.wait(90)
        while not self._stop.is_set():
            if self.cfg.get("enabled", True):
                self.sync()
            self._now.clear()
            self._now.wait(max(5, float(self.cfg.get("sync_interval_min", 20))) * 60)

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="weather").start()

    def stop(self):
        self._stop.set()
        self._now.set()


# ==================================================================== model ==
class WxModel:
    """Fair-value distribution for a station-day at any time t, using only data available at t."""

    def __init__(self, store: WxStore, run_lag_h: float = 3.0, obs_lag_min: float = 15.0, calib_days: int = 60,
                 cli_lag_h: float = 12.0, kernel_f: float = 0.8, shrink: float = 10.0):
        self.store = store
        self.run_lag = run_lag_h * 3600
        self.obs_lag = obs_lag_min * 60
        self.calib_days = calib_days
        self.cli_lag = cli_lag_h * 3600
        self.h = kernel_f
        self.shrink = shrink
        self.smap = store.series_map()
        self._nbs: dict[str, tuple[list, dict]] = {}
        self._obs: dict[str, tuple[list, list]] = {}
        self._cli: dict[str, dict] = {}
        self._resid: dict[tuple, list] = {}       # (date, cli) -> [(kind, bucket, e)]  (computed once per day)
        self._c: dict[tuple, dict] = {}           # (date, cli) -> {kind: c}
        self._dist_cache: dict = {}
        self._agg: dict = {}
        self._clis = None

    # ---- loaders (whole station history once; lookups are by time) ----
    def _load_nbs(self, icao):
        if icao not in self._nbs:
            runs: dict[int, list] = {}
            for r in self.store.db.query("SELECT runtime, ftime, tmp, txn FROM wx_nbs WHERE station=? ORDER BY runtime, ftime", (icao,)):
                runs.setdefault(r["runtime"], []).append((r["ftime"], r["tmp"], r["txn"]))
            self._nbs[icao] = (sorted(runs), runs)
        return self._nbs[icao]

    def _load_obs(self, aid):
        if aid not in self._obs:
            rows = self.store.db.query("SELECT ts, tmpf FROM wx_obs WHERE station=? ORDER BY ts", (aid,))
            self._obs[aid] = ([r["ts"] for r in rows], [r["tmpf"] for r in rows])
        return self._obs[aid]

    def _load_cli(self, icao):
        if icao not in self._cli:
            self._cli[icao] = {r["date"]: (r["high"], r["low"]) for r in
                               self.store.db.query("SELECT date, high, low FROM wx_cli WHERE station=?", (icao,))}
        return self._cli[icao]

    # ---- primitives ----
    def latest_run(self, icao, t):
        keys, runs = self._load_nbs(icao)
        i = bisect.bisect_right(keys, t - self.run_lag)
        return (keys[i - 1], runs[keys[i - 1]]) if i else (None, None)

    def obs_between(self, aid, a, b):
        ts, vs = self._load_obs(aid)
        i, j = bisect.bisect_left(ts, a), bisect.bisect_left(ts, b)
        return vs[i:j]

    def last_obs(self, aid, t):
        ts, vs = self._load_obs(aid)
        i = bisect.bisect_right(ts, t)
        return (ts[i - 1], vs[i - 1]) if i else (None, None)

    def forecast_x(self, cli, kind, d: dt.date, t: float):
        """Forecast extreme over the rest of the climate day, from the latest run published by t."""
        icao, aid, off = STATIONS[cli]
        ds, de = day_window(d, off)
        ws = max(t, ds)
        if ws >= de:
            return None, None
        rt, rows = self.latest_run(icao, t)
        vals = []
        if rows:
            for ft, tmp, txn in rows:
                if ws <= ft < de + 1 and tmp is not None:
                    vals.append(tmp)
                if txn is None:
                    continue
                hr = dt.datetime.fromtimestamp(ft, dt.timezone.utc).hour
                # 12-h daytime max (00Z row): its middle must fall inside the day and still be ahead of t
                if kind == "high" and hr == 0 and ds <= ft - 6 * 3600 < de and ft - 6 * 3600 >= ws:
                    vals.append(txn)
                # 12-h overnight min (12Z row): the min comes near the end of the period (around dawn)
                if kind == "low" and hr == 12 and ds + 2 * 3600 <= ft <= de + 3 * 3600 and ft - 2 * 3600 >= ws:
                    vals.append(txn)
        if vals:
            return (max(vals) if kind == "high" else min(vals)), rt
        lt, lv = self.last_obs(aid, t - self.obs_lag)
        if lv is not None and t - lt < 3 * 3600:
            return lv, None
        return None, None

    def observed_extreme(self, aid, kind, a, b):
        v = self.obs_between(aid, a, b)
        if not v:
            return None
        return max(v) if kind == "high" else min(v)

    # ---- calibration (per completed day; reused for every later decision) ----
    def _day_residuals(self, cli, d: dt.date):
        key = (d, cli)
        if key in self._resid:
            return self._resid[key], self._c[key]
        icao, aid, off = STATIONS[cli]
        ds, de = day_window(d, off)
        res, cs = [], {}
        clirow = self._load_cli(icao).get(d.isoformat())
        for kind in ("high", "low"):
            full = self.observed_extreme(aid, kind, ds, de)
            if clirow and full is not None:
                v = clirow[0] if kind == "high" else clirow[1]
                if v is not None:
                    cs[kind] = v - round(full)
            for bi, (_, hl) in enumerate(BUCKETS):
                t = de - hl * 3600
                x, _ = self.forecast_x(cli, kind, d, t)
                y = self.observed_extreme(aid, kind, max(t, ds), de)
                if x is not None and y is not None:
                    res.append((kind, bi, y - x))
        self._resid[key], self._c[key] = res, cs
        return res, cs

    def _aggregate(self, t):
        """Pooled residuals by (kind, bucket), per-station residuals and CLI gaps, from the calib_days most
        recent days whose CLI was published before t. Cached: it only changes when a new day qualifies."""
        offs = sorted({STATIONS[c][2] for c in self.clis()})
        today = dt.datetime.fromtimestamp(t, dt.timezone.utc).date()
        last = []
        for off in offs:
            d = today + dt.timedelta(days=1)
            while day_window(d, off)[1] + self.cli_lag > t:
                d -= dt.timedelta(days=1)
            last.append(d)
        key = tuple(last)
        if key in self._agg:
            self._agg[key] = self._agg.pop(key)        # LRU touch
            return self._agg[key]
        pooled: dict[tuple, list] = {}
        mine: dict[tuple, list] = {}
        gaps: dict[tuple, list] = {}
        newest = max(last)
        for k in range(self.calib_days + 1):
            d = newest - dt.timedelta(days=k)
            for c in self.clis():
                if d > last[offs.index(STATIONS[c][2])]:
                    continue
                res, cs = self._day_residuals(c, d)
                for kind, b, e in res:
                    pooled.setdefault((kind, b), []).append(e)
                    mine.setdefault((kind, b, c), []).append(e)
                for kind, g in cs.items():
                    gaps.setdefault((kind, c), []).append(g)
        self._agg[key] = (pooled, mine, gaps)
        while len(self._agg) > 16:
            self._agg.pop(next(iter(self._agg)))
        return self._agg[key]

    def calibration(self, kind, bi, cli, t):
        pooled, mine, gaps = self._aggregate(t)
        return pooled.get((kind, bi), []), mine.get((kind, bi, cli), []), gaps.get((kind, cli), [])

    def clis(self):
        if self._clis is None:
            self._clis = sorted({v["cli"] for v in self.smap.values() if v["mapped"]})
        return self._clis

    # ---- distribution ----
    def distribution(self, cli, kind, d: dt.date, t: float) -> dict | None:
        """{'dist': {temp: prob}, ...diagnostics} or None when there isn't enough data to price."""
        if cli not in STATIONS:
            return None
        key = (cli, kind, d, int(t // 600))
        if key in self._dist_cache:
            return self._dist_cache[key]
        icao, aid, off = STATIONS[cli]
        ds, de = day_window(d, off)
        hours_left = (de - t) / 3600
        x = rt = m = None
        bi, pooled, mine = 0, [], []
        if hours_left <= 0:
            m = self.observed_extreme(aid, kind, ds, de)
        else:
            bi = bucket_of(hours_left)
            if bi is None:
                self._dist_cache[key] = None
                return None
            x, rt = self.forecast_x(cli, kind, d, t)
            m = self.observed_extreme(aid, kind, ds, t - self.obs_lag) if t > ds else None
            pooled, mine, _ = self.calibration(kind, bi, cli, t)
        cgap = self.calibration(kind, 0, cli, t)[2]
        if hours_left > 0 and (x is None or len(pooled) < 30):
            self._dist_cache[key] = None
            return None
        # Y = x + bias + residual  (kernel-smoothed empirical), on whole degrees
        py: dict[int, float] = {}
        if hours_left > 0:
            mu = sum(pooled) / len(pooled)
            bias = (sum(mine) / len(mine) - mu) * len(mine) / (len(mine) + self.shrink) if mine else 0.0
            hist: dict[float, int] = {}
            for e in pooled:
                b = round(e * 2) / 2
                hist[b] = hist.get(b, 0) + 1
            n = len(pooled)
            lo = int(math.floor(x + bias + min(hist) - 4 * self.h))
            hi = int(math.ceil(x + bias + max(hist) + 4 * self.h))
            for k in range(lo, hi + 1):
                p = 0.0
                for b, cnt in hist.items():
                    cen = x + bias + b
                    p += cnt * (_phi((k + 0.5 - cen) / self.h) - _phi((k - 0.5 - cen) / self.h))
                if p > 1e-6 * n:
                    py[k] = p / n
        # O = obs-based day extreme
        po: dict[int, float] = {}
        mi = round(m) if m is not None else None
        if hours_left <= 0:
            if mi is None:
                self._dist_cache[key] = None
                return None
            po = {mi: 1.0}
        else:
            for k, p in py.items():
                o = k if mi is None else (max(k, mi) if kind == "high" else min(k, mi))
                po[o] = po.get(o, 0.0) + p
        # T = O + c
        if len(cgap) >= 10:
            cd: dict[int, float] = {}
            for c in cgap:
                c = max(-3, min(3, int(c)))
                cd[c] = cd.get(c, 0) + 1 / len(cgap)
        else:
            cd = {0: 1.0}
        dist: dict[int, float] = {}
        for o, p in po.items():
            for c, q in cd.items():
                dist[o + c] = dist.get(o + c, 0.0) + p * q
        tot = sum(dist.values())
        dist = {k: v for k, v in sorted(dist.items()) if v / tot > 1e-5}
        tot = sum(dist.values())
        dist = {k: v / tot for k, v in dist.items()}
        mean = sum(k * v for k, v in dist.items())
        out = {"dist": dist, "mean": round(mean, 2), "x": x, "obs_so_far": m, "run": rt, "bucket": bi,
               "hours_left": round(hours_left, 2), "n_resid": len(pooled), "n_station": len(mine), "n_gap": len(cgap)}
        self._dist_cache[key] = out
        if len(self._dist_cache) > 20000:
            self._dist_cache.clear()
        return out

    def fair(self, market: dict, t: float) -> dict | None:
        """P(YES) for a Kalshi temperature market at time t (or None)."""
        info = self.smap.get(market.get("series_ticker") or "")
        if not info or not info["mapped"]:
            return None
        d = event_date(market.get("event_ticker") or "")
        if not d:
            return None
        r = self.distribution(info["cli"], info["kind"], d, t)
        if not r:
            return None
        p = bucket_yes_prob(r["dist"], market.get("strike_type"), market.get("floor_strike"), market.get("cap_strike"))
        if p is None:
            return None
        return {**r, "p_yes": max(0.0, min(1.0, p)), "cli": info["cli"], "kind": info["kind"], "date": d.isoformat()}

    def cli_truth(self, market: dict) -> str | None:
        """What the NWS CLI value implies for this market ('yes'/'no') - for checking the settlement mapping."""
        info = self.smap.get(market.get("series_ticker") or "")
        d = event_date(market.get("event_ticker") or "")
        if not info or not info["mapped"] or not d:
            return None
        row = self._load_cli(STATIONS[info["cli"]][0]).get(d.isoformat())
        if not row:
            return None
        v = row[0] if info["kind"] == "high" else row[1]
        if v is None:
            return None
        p = bucket_yes_prob({int(v): 1.0}, market.get("strike_type"), market.get("floor_strike"), market.get("cap_strike"))
        return None if p is None else ("yes" if p > 0.5 else "no")


# ==================================================================== board ==
def board(wdb, model: "WxModel", now: float | None = None, max_days_ahead: int = 2) -> list[dict]:
    """Fair values for every open temperature event (today / tomorrow), next to the market's latest hourly
    quote from the warehouse. Read-only."""
    now = now or time.time()
    smap = {k: v for k, v in model.smap.items() if v["mapped"]}
    if not smap:
        return []
    ph = ",".join("?" * len(smap))
    rows = wdb.query(f"SELECT ticker, event_ticker, series_ticker, strike_type, floor_strike, cap_strike, sub_title, "
                     f"close_ts FROM wh_markets WHERE series_ticker IN ({ph}) AND close_ts > ? ORDER BY event_ticker, "
                     f"COALESCE(floor_strike, cap_strike - 100)", list(smap) + [now])
    out: dict[str, dict] = {}
    today = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
    for m in rows:
        d = event_date(m["event_ticker"])
        if not d or (d - today).days > max_days_ahead:
            continue
        info = smap[m["series_ticker"]]
        ev = out.get(m["event_ticker"])
        if ev is None:
            r = model.distribution(info["cli"], info["kind"], d, now)
            ev = out[m["event_ticker"]] = {
                "event": m["event_ticker"], "series": m["series_ticker"], "city": info["city"], "station": info["cli"],
                "kind": info["kind"], "date": d.isoformat(), "close_ts": m["close_ts"],
                "model": {k: v for k, v in (r or {}).items() if k != "dist"} if r else None,
                "dist": r["dist"] if r else None, "markets": []}
        q = wdb.query("SELECT end_ts, bid_c, ask_c, px_c FROM wh_candles WHERE ticker=? ORDER BY end_ts DESC LIMIT 1",
                      (m["ticker"],))
        q = q[0] if q else {}
        p = bucket_yes_prob(ev["dist"], m["strike_type"], m["floor_strike"], m["cap_strike"]) if ev["dist"] else None
        bid, ask = q.get("bid_c"), q.get("ask_c")
        ev["markets"].append({"ticker": m["ticker"], "sub": m["sub_title"], "fair": None if p is None else round(p, 4),
                              "bid": bid, "ask": ask, "quote_ts": q.get("end_ts"),
                              "edge_buy_yes": None if p is None or not ask else round(p - ask, 4),
                              "edge_buy_no": None if p is None or not bid else round(bid - p, 4)})
    return sorted(out.values(), key=lambda e: (e["date"], e["series"]))


# =============================================================== evaluation ==
def evaluate(wdb, leads_h=(36, 24, 12, 6, 3, 1), calib_days: int = 60, run_lag_h: float = 3.0,
             max_events: int | None = None, progress=None) -> dict:
    """Out-of-sample scorecard, before any trading assumptions:
      - settlement mapping: does the NWS CLI value reproduce Kalshi's actual result?
      - accuracy: Brier score of the model's P(YES) vs the market's own price (candle mid / last) at the same
        moment, for every settled bucket, at fixed hours before the climate day ends.
    Lower Brier = better. 'model_better_pct' = share of buckets where the model's error was smaller."""
    store = WxStore(wdb)
    model = WxModel(store, run_lag_h=run_lag_h, calib_days=calib_days)
    smap = {k: v for k, v in model.smap.items() if v["mapped"]}
    if not smap:
        return {"error": "no temperature series mapped to a station"}
    ph = ",".join("?" * len(smap))
    mkts = wdb.query(f"SELECT ticker, event_ticker, series_ticker, strike_type, floor_strike, cap_strike, result, close_ts "
                     f"FROM wh_markets WHERE series_ticker IN ({ph}) AND result IN ('yes','no') ORDER BY close_ts",
                     list(smap))
    events: dict[str, list] = {}
    for m in mkts:
        events.setdefault(m["event_ticker"], []).append(m)
    keys = list(events)[: max_events or None]
    agree = disagree = nocli = 0
    mism = []
    score = {h: {"n": 0, "model": 0.0, "market": 0.0, "model_better": 0, "ll_model": 0.0, "ll_market": 0.0,
                 "calib": {}} for h in leads_h}
    priced = unpriced = 0
    for i, ev in enumerate(keys):
        ms = events[ev]
        info = smap[ms[0]["series_ticker"]]
        d = event_date(ev)
        if not d:
            continue
        de = day_window(d, info["offset"])[1]
        for m in ms:
            truth = model.cli_truth(m)
            if truth is None:
                nocli += 1
            elif truth == m["result"]:
                agree += 1
            else:
                disagree += 1
                if len(mism) < 40:
                    row = model._load_cli(info["icao"]).get(d.isoformat())
                    mism.append({"ticker": m["ticker"], "kalshi": m["result"], "cli_value": row})
        for h in leads_h:
            t = de - h * 3600
            if t > (ms[0]["close_ts"] or t):
                continue
            for m in ms:
                fv = model.fair(m, t)
                bar = wdb.query("SELECT bid_c, ask_c, px_c FROM wh_candles WHERE ticker=? AND end_ts<=? "
                                "ORDER BY end_ts DESC LIMIT 1", (m["ticker"], t))
                if not fv or not bar:
                    unpriced += 1
                    continue
                b = bar[0]
                if b["bid_c"] is not None and b["ask_c"] is not None and 0 < b["bid_c"] < b["ask_c"] < 1:
                    mk = (b["bid_c"] + b["ask_c"]) / 2
                elif b["px_c"] is not None:
                    mk = b["px_c"]
                else:
                    unpriced += 1
                    continue
                priced += 1
                y = 1.0 if m["result"] == "yes" else 0.0
                p = fv["p_yes"]
                s = score[h]
                s["n"] += 1
                em, ek = (p - y) ** 2, (mk - y) ** 2
                s["model"] += em
                s["market"] += ek
                s["model_better"] += 1 if em < ek else 0
                cl = lambda q: min(1 - 1e-3, max(1e-3, q))
                s["ll_model"] -= math.log(cl(p) if y else 1 - cl(p))
                s["ll_market"] -= math.log(cl(mk) if y else 1 - cl(mk))
                bk = f"{min(9, int(p * 10)) / 10:.1f}"
                c = s["calib"].setdefault(bk, [0, 0.0, 0.0])
                c[0] += 1
                c[1] += p
                c[2] += y
        if progress and i % 200 == 0:
            progress(i, len(keys))
    out = {}
    for h, s in score.items():
        n = s["n"]
        out[str(h)] = {"n": n, "brier_model": round(s["model"] / n, 5) if n else None,
                       "brier_market": round(s["market"] / n, 5) if n else None,
                       "logloss_model": round(s["ll_model"] / n, 4) if n else None,
                       "logloss_market": round(s["ll_market"] / n, 4) if n else None,
                       "model_better_pct": round(100 * s["model_better"] / n, 1) if n else None,
                       "calibration": {k: {"n": v[0], "avg_p": round(v[1] / v[0], 3), "hit": round(v[2] / v[0], 3)}
                                       for k, v in sorted(s["calib"].items())}}
    return {"events": len(keys), "markets": len(mkts),
            "settlement_check": {"cli_agrees": agree, "cli_disagrees": disagree, "no_cli": nocli,
                                 "agree_pct": round(100 * agree / (agree + disagree), 2) if agree + disagree else None,
                                 "examples": mism},
            "priced": priced, "unpriced": unpriced, "by_hours_before_day_end": out,
            "series": {k: v["cli"] for k, v in smap.items()},
            "unmapped": sorted({v["cli"] for v in model.smap.values() if not v["mapped"]})}


if __name__ == "__main__":
    import argparse
    import sys

    from .warehouse import WarehouseDB
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--sync", action="store_true", help="backfill/refresh weather data")
    ap.add_argument("--eval", action="store_true", help="out-of-sample scorecard")
    ap.add_argument("--out", default="")
    ap.add_argument("--store", action="store_true", help="save the scorecard in the warehouse (wx_eval)")
    a = ap.parse_args()
    wdb = WarehouseDB(a.db)
    res = {}
    if a.sync:
        col = WxCollector(None, WxStore(wdb))
        res["sync"] = col.sync()
        res["collector"] = col.status()
    if a.eval:
        t0 = time.time()
        if a.store:
            wdb.put("wx_eval", {"status": "running", "started": t0})
        try:
            res["eval"] = evaluate(wdb, progress=lambda i, n: (wdb.put("wx_eval", {"status": "running", "started": t0,
                                                                                    "progress": f"{i}/{n}"}) if a.store else None))
            if a.store:
                wdb.put("wx_eval", {"status": "done", "started": t0, "duration": round(time.time() - t0, 1), **res["eval"]})
        except Exception as e:
            if a.store:
                wdb.put("wx_eval", {"status": "error", "started": t0, "error": f"{e}\n{traceback.format_exc()[-1500:]}"})
            raise
    txt = json.dumps(res, indent=1, default=str)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(txt)
    else:
        print(txt)
