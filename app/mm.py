"""Phase 2.3: market-making study. READ-ONLY: uses Kalshi production PUBLIC data and places nothing.

1) Landscape scan
   Every active liquidity-incentive program (all pages), the market it pays on and that market's
   order book. For each quote size it estimates what a post-only bid at the best YES bid and the best
   NO bid would earn per day under Kalshi's published scoring, and the capital it ties up.

   Scoring (Kalshi LIP, CFTC filing of Jul 15 2026, Appendix A), per side and per snapshot:
     * walk the bids from the best price down, whole price levels at a time, until the cumulative size
       reaches Target Size; those are the qualifying bids
     * Reference Price = the first level where the cumulative size reaches Target Size / 5
     * score of a bid = size x DiscountFactor^N, N = ticks below the Reference Price (0 at or above it)
     * each side is normalised to 1; your snapshot score = your YES share + your NO share
     * snapshots without resting size >= Target Size on BOTH sides are excluded (pay nothing)
     * payout = Period Reward x (your score summed over snapshots) / (2 x all snapshots); paid only if >= $1
   The estimate is one snapshot of today's book. It ignores fills (that is what the shadow measures).

2) Shadow quoter (paper trading on live production data)
   Re-runs the liquidity quoter's own rules (join best YES bid and best NO bid, inventory cap, jump
   pull + cool-down, stop before settlement, re-placing every `requeue_sec`) on the markets picked by
   the scan, polling the public book and public trades. Nothing is sent to Kalshi.
     * rewards accrue every poll from the scoring above, with your virtual orders inserted
     * fills come only from real later trades:
         queue  (conservative) you join the BACK of the queue at your price; you fill when a trade
                prints through your price, or when trades at your price on your side exceed the size
                that was ahead of you (that size only shrinks with trades, or when the level itself
                shows less size than that)
         touch  (optimistic) any trade at your price on your side fills you, ignoring the queue
     * maker fees per the series' fee type; inventory marked to the mid; settled markets pay $1/0
     * markouts: mid 1 / 10 / 60 minutes after each fill vs your price = adverse selection
   Net = rewards + mark-to-market trading P&L - fees, under both fill models.
"""
from __future__ import annotations

import bisect
import json
import math
import threading
import time
import traceback

from .fees import fee
from .kalshi_client import KalshiClient, TokenBucket
from .warehouse import PROD_URL, iso_ts

SCHEMA = """
CREATE TABLE IF NOT EXISTS mm_scans (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, status TEXT, progress TEXT,
    params TEXT, summary TEXT, error TEXT, duration REAL);
CREATE TABLE IF NOT EXISTS mm_scan_rows (scan_id INTEGER, ticker TEXT, data TEXT, PRIMARY KEY (scan_id, ticker));
CREATE TABLE IF NOT EXISTS mm_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, params TEXT, status TEXT,
    stopped_ts REAL, scan_id INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS mm_markets (run_id INTEGER, ticker TEXT, info TEXT, PRIMARY KEY (run_id, ticker));
CREATE TABLE IF NOT EXISTS mm_state (run_id INTEGER, ticker TEXT, model TEXT, state TEXT, updated REAL,
    PRIMARY KEY (run_id, ticker, model));
CREATE TABLE IF NOT EXISTS mm_fills (run_id INTEGER, ticker TEXT, model TEXT, ts REAL, side TEXT, price REAL,
    count REAL, fee REAL, mid REAL, kind TEXT);
CREATE INDEX IF NOT EXISTS mm_fills_r ON mm_fills(run_id, ticker, ts);
CREATE TABLE IF NOT EXISTS mm_mids (run_id INTEGER, ticker TEXT, ts REAL, mid REAL, yes_bid REAL, yes_ask REAL);
CREATE INDEX IF NOT EXISTS mm_mids_r ON mm_mids(run_id, ticker, ts);
"""

_SCHEMA_DONE: set = set()
TICK = 0.01
EPS = 1e-9
MODELS = ("queue", "touch")
SIZES = (10, 25, 50, 100)
BUDGETS = (100, 250, 500, 1000, 2500)
MARKOUTS = (60, 600, 3600)


def ensure_schema(wdb):
    if id(wdb) in _SCHEMA_DONE:
        return
    with wdb.lock:
        wdb.conn.executescript(SCHEMA)
    _SCHEMA_DONE.add(id(wdb))


def fnum(x) -> float:
    try:
        return float(x) if x not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def book_levels(raw) -> list[tuple[float, float]]:
    """[[price_str, size_str], ...] -> [(price, size)] best (highest) first, merged by price."""
    agg: dict[float, float] = {}
    for p, c in raw or []:
        p, c = round(fnum(p), 4), fnum(c)
        if c > 0 and 0 < p < 1:
            agg[p] = agg.get(p, 0.0) + c
    return sorted(agg.items(), key=lambda x: -x[0])


def side_score(levels, target: float, df: float, ours: tuple[float, float] | None = None) -> dict:
    """Kalshi LIP scoring for one side of one snapshot.
    levels: other participants' bids on this side [(price, size)] best first. ours: (price, size) or None.
    Returns {share: your normalised score (0..1), eligible: side has >= target resting, total: size incl. yours,
             ref: reference price}."""
    lv = {p: c for p, c in levels}
    mine = 0.0
    if ours and ours[1] > 0:
        op = round(ours[0], 4)
        lv[op] = lv.get(op, 0.0) + ours[1]
        mine = ours[1]
    prices = sorted(lv, reverse=True)
    total = sum(lv.values())
    if not prices:
        return {"share": 0.0, "eligible": False, "total": 0.0, "ref": None}
    cum, ref, qual = 0.0, None, []
    for p in prices:
        cum += lv[p]
        qual.append(p)
        if ref is None and cum >= target / 5 - EPS:
            ref = p
        if cum >= target - EPS:
            break
    if ref is None:
        ref = qual[-1]
    tot_s, my_s = 0.0, 0.0
    for p in qual:
        n = max(0, int(round((ref - p) / TICK))) if p < ref - EPS else 0
        w = df ** n
        tot_s += lv[p] * w
        if ours and mine and abs(p - round(ours[0], 4)) < EPS:
            my_s = mine * w
    return {"share": (my_s / tot_s) if tot_s > 0 else 0.0, "eligible": total >= target - EPS, "total": total, "ref": ref}


def snapshot_score(yes_lv, no_lv, target, df, our_yes=None, our_no=None) -> dict:
    y = side_score(yes_lv, target, df, our_yes)
    n = side_score(no_lv, target, df, our_no)
    elig = y["eligible"] and n["eligible"]
    return {"eligible": elig, "share_yes": y["share"], "share_no": n["share"],
            "score": (y["share"] + n["share"]) if elig else 0.0}   # 0..2 per snapshot


def aggregate_programs(progs: list[dict], now: float) -> dict[str, dict]:
    """Active liquidity programs -> per market: $/day, target, discount factor, days left, $/account cap."""
    pools: dict[str, dict] = {}
    for p in progs:
        if (p.get("incentive_type") or "liquidity") != "liquidity":
            continue
        start, end = iso_ts(p.get("start_date")), iso_ts(p.get("end_date"))
        if not start or not end or end <= now or start > now:
            continue
        days = max(1 / 24, (end - start) / 86400)
        x = pools.setdefault(p["market_ticker"], {"daily": 0.0, "end": 0.0, "target": 0.0, "df": 1.0,
                                                   "programs": 0, "max_per_account": None, "period_reward": 0.0})
        reward = fnum(p.get("period_reward")) / 10000
        x["daily"] += reward / days
        x["period_reward"] += reward
        x["end"] = max(x["end"], end)
        x["target"] = max(x["target"], fnum(p.get("target_size_fp")))
        if p.get("discount_factor_bps") not in (None, ""):
            x["df"] = fnum(p.get("discount_factor_bps")) / 10000
        if p.get("max_reward_per_account") not in (None, ""):
            cap = fnum(p["max_reward_per_account"]) / 10000
            x["max_per_account"] = cap if x["max_per_account"] is None else min(x["max_per_account"], cap)
        x["programs"] += 1
    for v in pools.values():
        v["days_left"] = max(0.0, (v["end"] - now) / 86400)
        if v["target"] <= 0:
            v["target"] = 100.0
    return pools


def quoter_eligible(m: dict, q: dict, now: float) -> tuple[bool, str]:
    """The live quoter's market filter (same rules as Quoter._eligible)."""
    if m.get("status") != "active":
        return False, "not active"
    if (m.get("market_type") or "binary") != "binary":
        return False, "not binary"
    ends = [x for x in (iso_ts(m.get("expected_expiration_time")), iso_ts(m.get("close_time"))) if x]
    if ends and min(ends) - now < float(q.get("pull_hours", 6)) * 3600:
        return False, "settles soon"
    yb, ya = fnum(m.get("yes_bid_dollars")), fnum(m.get("yes_ask_dollars"))
    if yb <= 0 or ya <= 0:
        return False, "one-sided"
    if ya - yb > float(q.get("max_spread", 0.06)) + EPS:
        return False, "spread too wide"
    mid = (yb + ya) / 2
    if not float(q.get("min_mid", 0.15)) - EPS <= mid <= float(q.get("max_mid", 0.85)) + EPS:
        return False, "mid out of range"
    return True, ""


def allocate(rows: list[dict], size: int, budgets=BUDGETS) -> list[dict]:
    """Greedy: best estimated $/day per $ of capital first, until the budget is used."""
    k = str(size)
    cands = sorted((r for r in rows if r.get("quoter_ok") and r["sizes"][k]["est_daily"] > 0 and r["sizes"][k]["capital"] > 0),
                   key=lambda r: -r["sizes"][k]["est_daily"] / r["sizes"][k]["capital"])
    out = []
    for b in budgets:
        used, est, n = 0.0, 0.0, 0
        for r in cands:
            s = r["sizes"][k]
            if used + s["capital"] > b:
                continue
            used += s["capital"]
            est += s["est_daily"]
            n += 1
        out.append({"budget": b, "markets": n, "capital": round(used, 2), "est_daily": round(est, 2)})
    return out


def fetch_programs(client, max_pages=200) -> tuple[list[dict], int]:
    progs, cur, pages = [], None, 0
    while True:
        page = client.request("GET", "/incentive_programs", {"status": "active", "type": "liquidity", "limit": 1000, "cursor": cur})
        progs += page.get("incentive_programs") or []
        pages += 1
        cur = page.get("next_cursor")
        if not cur or pages >= max_pages:
            return progs, pages


def rank_markets(client, q: dict, sizes, now: float, prefilter: bool = False, min_pool: float = 0.0,
                 progress=None) -> tuple[list[dict], dict]:
    """Score every market with an active liquidity program at each quote size (one snapshot of the book).
    prefilter=True (the live quoter) only fetches books for markets that pass the quoter's filters on their
    quoted best bid/ask, which cuts the requests ~5x. Returns (rows, info)."""
    prog = progress or (lambda msg: None)
    prog("incentive programs")
    progs, pages = fetch_programs(client)
    pools = {t: v for t, v in aggregate_programs(progs, now).items() if v["daily"] >= min_pool}
    tickers = sorted(pools)
    mk: dict[str, dict] = {}
    for i in range(0, len(tickers), 100):
        prog(f"markets {i}/{len(tickers)}")
        for m in client.markets(status=None, tickers=tickers[i:i + 100], exclude_combos=False):
            mk[m["ticker"]] = m
    live = [t for t in tickers if mk.get(t, {}).get("status") == "active"
            and (mk[t].get("market_type") or "binary") == "binary"]
    if prefilter:
        live = [t for t in live if quoter_eligible(mk[t], q, now)[0]]
    books: dict[str, dict] = {}
    for i in range(0, len(live), 20):
        if i % 400 == 0:
            prog(f"order books {i}/{len(live)}")
        books.update(client.orderbooks(live[i:i + 20]))
    rows = []
    for t in live:
        m, v, ob = mk[t], pools[t], books.get(t) or {}
        yes_lv, no_lv = book_levels(ob.get("yes_dollars")), book_levels(ob.get("no_dollars"))
        yb = yes_lv[0][0] if yes_lv else 0.0
        nb = no_lv[0][0] if no_lv else 0.0
        ya = 1 - nb if nb else 0.0
        ok, why = quoter_eligible({**m, "yes_bid_dollars": yb, "yes_ask_dollars": ya}, q, now)
        per = {}
        for sz in sizes:
            if yb and nb:
                sc = snapshot_score(yes_lv, no_lv, v["target"], v["df"], (yb, sz), (nb, sz))
            else:
                sc = {"eligible": False, "share_yes": 0.0, "share_no": 0.0, "score": 0.0}
            est = v["daily"] * sc["score"] / 2
            if v["max_per_account"] is not None:
                est = min(est, v["max_per_account"] / max(1.0, v["days_left"]))
            if est * min(v["days_left"], 31) < 1:      # Kalshi pays nothing under $1 per market program
                est = 0.0
            per[str(sz)] = {"share_yes": round(sc["share_yes"], 4), "share_no": round(sc["share_no"], 4),
                            "eligible": sc["eligible"], "est_daily": round(est, 4), "capital": round(sz * (yb + nb), 2)}
        rows.append({"ticker": t, "series": t.split("-")[0], "event": m.get("event_ticker"),
                     "title": m.get("title"), "sub": m.get("yes_sub_title"),
                     "daily": round(v["daily"], 4), "target": v["target"], "df": v["df"], "days_left": round(v["days_left"], 2),
                     "max_per_account": v["max_per_account"], "yes_bid": yb, "yes_ask": ya,
                     "mid": round((yb + ya) / 2, 4) if yb and ya else None,
                     "spread": round(ya - yb, 4) if yb and ya else None,
                     "depth_yes": round(sum(c for _, c in yes_lv), 2), "depth_no": round(sum(c for _, c in no_lv), 2),
                     "close_ts": iso_ts(m.get("close_time")), "quoter_ok": ok, "why_not": why,
                     "volume_24h": fnum(m.get("volume_24h_fp") or m.get("volume_24h")), "sizes": per})
    return rows, {"programs": len(progs), "pages": pages, "pools": pools, "live": live}


def pick_for_quoter(rows: list[dict], size: int, max_markets: int, max_capital: float, max_volume_24h: float = 0) -> list[dict]:
    """Best estimated reward per $ of capital first, until max_markets or the capital cap is reached."""
    k = str(size)
    cands = [r for r in rows if r["quoter_ok"] and r["sizes"][k]["est_daily"] > 0 and r["sizes"][k]["capital"] > 0
             and (not max_volume_24h or r["volume_24h"] <= max_volume_24h)]
    cands.sort(key=lambda r: -r["sizes"][k]["est_daily"] / r["sizes"][k]["capital"])
    out, used = [], 0.0
    for r in cands:
        if len(out) >= max_markets:
            break
        c = r["sizes"][k]["capital"]
        if used + c > max_capital + EPS:
            continue
        used += c
        out.append(r)
    return out


# ------------------------------------------------------------------ scan --
class Scanner:
    def __init__(self, wdb, client: KalshiClient | None = None, req_per_sec: float = 5, clock=time.time):
        ensure_schema(wdb)
        self.db = wdb
        self.client = client or KalshiClient(PROD_URL)
        self.client.read_bucket = TokenBucket(rate=req_per_sec * 10, capacity=max(10, req_per_sec * 10))
        self.clock = clock
        self.lock = threading.Lock()
        self.running = False

    def start(self, params: dict | None = None) -> int:
        with self.lock:
            if self.running:
                raise RuntimeError("a scan is already running")
            self.running = True
        cur = self.db.execute("INSERT INTO mm_scans(ts,status,progress,params) VALUES(?, 'running', '', ?)",
                              (self.clock(), json.dumps(params or {})))
        sid = cur.lastrowid
        threading.Thread(target=self._run_safe, args=(sid, params or {}), daemon=True, name="mm-scan").start()
        return sid

    def _run_safe(self, sid, params):
        try:
            self.run(sid, params)
        finally:
            self.running = False

    def run(self, sid: int, params: dict) -> dict:
        t0 = time.time()

        def prog(msg):
            self.db.execute("UPDATE mm_scans SET progress=? WHERE id=?", (msg, sid))
        try:
            q = {"pull_hours": 6, "max_spread": 0.06, "min_mid": 0.15, "max_mid": 0.85, **(params.get("quoter") or {})}
            sizes = tuple(int(s) for s in params.get("sizes") or SIZES)
            now = self.clock()
            rows, info = rank_markets(self.client, q, sizes, now, progress=prog)
            progs, pages, pools, live = info["programs"], info["pages"], info["pools"], info["live"]
            with self.db.lock:
                self.db.conn.executemany("INSERT OR REPLACE INTO mm_scan_rows VALUES(?,?,?)",
                                         [(sid, r["ticker"], json.dumps(r)) for r in rows])
                self.db.conn.commit()
            total_pool = sum(v["daily"] for v in pools.values())
            summary = {
                "programs": progs, "program_pages": pages, "markets_with_programs": len(pools),
                "pool_daily_total": round(total_pool, 2), "active_binary": len(live),
                "quoter_ok": sum(1 for r in rows if r["quoter_ok"]),
                "why_not": _count(r["why_not"] for r in rows if not r["quoter_ok"]),
                "eligible_now": {str(s): sum(1 for r in rows if r["sizes"][str(s)]["eligible"]) for s in sizes},
                "allocation": {str(s): allocate(rows, s) for s in sizes},
                "program_end_dates": _count(time.strftime("%Y-%m-%d", time.gmtime(v["end"])) for v in pools.values()),
                "sizes": list(sizes), "quoter_rules": q,
            }
            self.db.execute("UPDATE mm_scans SET status='done', progress='', summary=?, duration=? WHERE id=?",
                            (json.dumps(summary), round(time.time() - t0, 1), sid))
            return summary
        except Exception as e:
            self.db.execute("UPDATE mm_scans SET status='error', error=?, duration=? WHERE id=?",
                            (f"{e}\n{traceback.format_exc()[-1200:]}", round(time.time() - t0, 1), sid))
            raise

    def latest(self, done_only=False) -> dict | None:
        r = self.db.query("SELECT * FROM mm_scans " + ("WHERE status='done' " if done_only else "") + "ORDER BY id DESC LIMIT 1")
        if not r:
            return None
        r = r[0]
        r["summary"] = json.loads(r["summary"] or "null")
        r["params"] = json.loads(r["params"] or "{}")
        return r

    def rows(self, sid: int, size: int = 10, limit: int = 100, sort: str = "per_capital", quoter_ok_only=True) -> list[dict]:
        rows = [json.loads(x["data"]) for x in self.db.query("SELECT data FROM mm_scan_rows WHERE scan_id=?", (sid,))]
        k = str(size)
        rows = [r for r in rows if k in r["sizes"] and (r["quoter_ok"] or not quoter_ok_only)]
        if sort == "daily":
            rows.sort(key=lambda r: -r["sizes"][k]["est_daily"])
        elif sort == "pool":
            rows.sort(key=lambda r: -r["daily"])
        else:
            rows.sort(key=lambda r: -(r["sizes"][k]["est_daily"] / r["sizes"][k]["capital"]) if r["sizes"][k]["capital"] else 0)
        return rows[:limit]


def _count(it) -> dict:
    out: dict = {}
    for x in it:
        out[x] = out.get(x, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1])[:30])


# ---------------------------------------------------------------- shadow --
class SimQuoter:
    """The liquidity quoter's rules on one market, with virtual orders. Pure logic, no I/O."""

    def __init__(self, info: dict, p: dict, model: str, state: dict | None = None):
        self.info, self.p, self.model = info, p, model
        self.s = state or {"pos_yes": 0.0, "pos_no": 0.0, "cost": 0.0, "fees": 0.0, "reward": 0.0, "total_sec": 0.0,
                           "elig_sec": 0.0, "quoted_sec": 0.0, "fills": 0, "contracts": 0.0, "quotes": {},
                           "cooldown_until": 0.0, "mid_hist": [], "last_ts": None, "jumps": 0, "settled": None,
                           "max_capital": 0.0}

    # fills ------------------------------------------------------------------
    def apply_trades(self, trades: list[dict]) -> list[dict]:
        out = []
        for t in trades:
            yp, cnt, ts = t.get("yes_price"), t.get("count") or 0.0, t.get("ts") or 0.0
            if yp is None or cnt <= 0:
                continue
            for side in ("yes", "no"):
                q = self.s["quotes"].get(side)
                if not q or q["left"] <= 0 or ts <= q["placed"]:
                    continue
                if side == "yes":
                    through = yp < q["px"] - EPS
                    at = abs(yp - q["px"]) < EPS and t.get("taker_book_side") == "ask"
                else:
                    through = yp > 1 - q["px"] + EPS
                    at = abs(yp - (1 - q["px"])) < EPS and t.get("taker_book_side") == "bid"
                n = 0.0
                if through:
                    n = q["left"]
                elif at:
                    if self.model == "touch":
                        n = min(q["left"], cnt)
                    else:
                        q["ahead"] -= cnt
                        if q["ahead"] < 0:
                            n = min(q["left"], -q["ahead"])
                            q["ahead"] = 0.0
                if n > 0:
                    q["left"] -= n
                    out.append(self._fill(side, q["px"], n, ts, "through" if through else "at"))
        return out

    def _fill(self, side, px, n, ts, kind) -> dict:
        fe = float(fee(px, n, self.info.get("fee_type") or "quadratic", self.info.get("fee_multiplier") or 1, maker=True))
        self.s["pos_" + side] += n
        self.s["cost"] += px * n
        self.s["fees"] += fe
        self.s["fills"] += 1
        self.s["contracts"] += n
        return {"ts": ts, "side": side, "price": px, "count": n, "fee": fe, "kind": kind}

    # rewards ----------------------------------------------------------------
    def accrue(self, now: float, yes_lv, no_lv, open_: bool, max_dt: float):
        last = self.s["last_ts"]
        self.s["last_ts"] = now
        if last is None:
            return
        dt = max(0.0, min(now - last, max_dt))
        self.s["total_sec"] += dt
        if not open_ or dt <= 0:
            return
        qy, qn = self.s["quotes"].get("yes"), self.s["quotes"].get("no")
        oy = (qy["px"], qy["left"]) if qy and qy["left"] > 0 else None
        on = (qn["px"], qn["left"]) if qn and qn["left"] > 0 else None
        sc = snapshot_score(yes_lv, no_lv, self.info["target"], self.info["df"], oy, on)
        if sc["eligible"]:
            self.s["elig_sec"] += dt
        if oy or on:
            self.s["quoted_sec"] += dt
        self.s["reward"] += self.info["daily"] / 86400 * dt * sc["score"] / 2

    # quoting ----------------------------------------------------------------
    def _jumped(self, now, mid) -> bool:
        h = self.s["mid_hist"]
        window = max(float(self.p.get("refresh_sec", 15)), float(self.p.get("poll_sec", 10)))
        ref = [m for ts, m in h if now - ts <= window]
        if h:
            ref.append(h[-1][1])
        h.append((now, mid))
        del h[:-50]
        thr = float(self.p.get("jump_cents", 4)) / 100
        if ref and max(abs(mid - m) for m in ref) >= thr - EPS:
            self.s["mid_hist"] = [(now, mid)]
            return True
        return False

    def requote(self, now: float, market: dict, yes_lv, no_lv):
        s, p = self.s, self.p
        yb = yes_lv[0][0] if yes_lv else 0.0
        nb = no_lv[0][0] if no_lv else 0.0
        ok, _ = quoter_eligible({**market, "yes_bid_dollars": yb, "yes_ask_dollars": (1 - nb) if nb else 0.0}, p, now)
        if not ok or s["settled"]:
            s["quotes"] = {}
            return
        mid = (yb + 1 - nb) / 2
        if self._jumped(now, mid):
            s["quotes"] = {}
            s["cooldown_until"] = now + float(p.get("cooldown_min", 10)) * 60
            s["jumps"] += 1
            return
        if s["cooldown_until"] > now:
            return
        size, inv = float(p.get("size", 10)), float(p.get("max_inventory", 30))
        net = s["pos_yes"] - s["pos_no"]
        want = {}
        if net < inv:
            want["yes"] = (yb, dict(yes_lv).get(yb, 0.0))
        if -net < inv:
            want["no"] = (nb, dict(no_lv).get(nb, 0.0))
        requeue = float(p.get("requeue_sec", 60))
        for side in ("yes", "no"):
            q = s["quotes"].get(side)
            if side not in want:
                s["quotes"].pop(side, None)
                continue
            px, level = want[side]
            if q and abs(q["px"] - px) < EPS and q["left"] > 0 and now - q["placed"] < requeue:
                q["ahead"] = min(q["ahead"], level)      # the level cannot hold more than is shown
                continue
            s["quotes"][side] = {"px": px, "left": size, "ahead": level, "placed": now}
        cap = sum(q["px"] * q["left"] for q in s["quotes"].values()) + s["cost"]
        s["max_capital"] = max(s["max_capital"], cap)

    def settle(self, result: str):
        if result in ("yes", "no") and not self.s["settled"]:
            self.s["settled"] = result
            self.s["quotes"] = {}

    def value(self, mid: float | None) -> float:
        s = self.s
        if s["settled"] == "yes":
            return s["pos_yes"]
        if s["settled"] == "no":
            return s["pos_no"]
        if mid is None:
            return 0.0
        return s["pos_yes"] * mid + s["pos_no"] * (1 - mid)


class Shadow:
    def __init__(self, wdb, client: KalshiClient | None = None, req_per_sec: float = 4, clock=time.time, sleep=None):
        ensure_schema(wdb)
        self.db = wdb
        self.client = client or KalshiClient(PROD_URL)
        self.client.read_bucket = TokenBucket(rate=req_per_sec * 10, capacity=max(10, req_per_sec * 10))
        self.clock = clock
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.run_id: int | None = None
        self.p: dict = {}
        self.markets: dict[str, dict] = {}
        self.sims: dict[tuple[str, str], SimQuoter] = {}
        self.cursor: dict[str, dict] = {}       # ticker -> {"ts": last trade ts, "seen": [ids]}
        self.mkt: dict[str, dict] = {}          # latest market objects
        self.mkt_ts = 0.0
        self.last_mid: dict[str, float] = {}
        self.mid_written: dict[str, tuple] = {}
        self.persist_ts = 0.0
        self.state = {"running": False, "polls": 0, "errors": 0, "last_error": None, "last_poll": None, "requests": 0}
        self.lock = threading.RLock()

    # ---- lifecycle -----------------------------------------------------------
    def start(self, scanner: Scanner, params: dict) -> int:
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise RuntimeError("a shadow run is already running; stop it first")
            sc = scanner.latest(done_only=True)
            if not sc:
                raise RuntimeError("run a scan first")
            p = {"n_markets": 20, "size": 10, "poll_sec": 10, "max_days": 21, "max_inventory": 30, "pull_hours": 6,
                 "jump_cents": 4, "cooldown_min": 10, "refresh_sec": 15, "min_mid": 0.15, "max_mid": 0.85,
                 "max_spread": 0.06, "requeue_sec": 60, "per_series": 3, **params}
            size = int(p["size"])
            k = str(size if str(size) in {str(s) for s in sc["summary"]["sizes"]} else sc["summary"]["sizes"][0])
            rows = scanner.rows(sc["id"], int(k), limit=100000)
            picked, per_series = [], {}
            for r in rows:
                if r["sizes"][k]["est_daily"] <= 0:
                    continue
                if per_series.get(r["series"], 0) >= int(p["per_series"]):
                    continue                    # spread the study over different series
                per_series[r["series"]] = per_series.get(r["series"], 0) + 1
                picked.append(r)
                if len(picked) >= int(p["n_markets"]):
                    break
            if not picked:
                raise RuntimeError("the latest scan has no market with an estimated reward at this size")
            fee_types: dict[str, dict] = {}
            for r in picked:
                if r["series"] not in fee_types:
                    try:
                        s = self.client.series(r["series"]) or {}
                    except Exception:
                        s = {}
                    fee_types[r["series"]] = {"fee_type": s.get("fee_type") or "quadratic",
                                              "fee_multiplier": fnum(s.get("fee_multiplier")) or 1.0}
            cur = self.db.execute("INSERT INTO mm_runs(ts,params,status,scan_id) VALUES(?,?, 'running', ?)",
                                  (self.clock(), json.dumps(p), sc["id"]))
            rid = cur.lastrowid
            for r in picked:
                info = {"ticker": r["ticker"], "title": r["title"], "sub": r["sub"], "series": r["series"],
                        "daily": r["daily"], "target": r["target"], "df": r["df"], "scan_est_daily": r["sizes"][k]["est_daily"],
                        "scan_capital": r["sizes"][k]["capital"], **fee_types[r["series"]]}
                self.db.execute("INSERT OR REPLACE INTO mm_markets VALUES(?,?,?)", (rid, r["ticker"], json.dumps(info)))
            self._load(rid)
            self._spawn()
            return rid

    def resume(self) -> int | None:
        """Continue a run that was going when the app stopped (open quotes are dropped: the real quoter's
        orders would have expired too)."""
        r = self.db.query("SELECT id FROM mm_runs WHERE status='running' ORDER BY id DESC LIMIT 1")
        if not r or (self.thread and self.thread.is_alive()):
            return None
        self._load(r[0]["id"], resumed=True)
        self._spawn()
        return self.run_id

    def stop(self, note="stopped"):
        self._stop.set()
        if self.thread:
            self.thread.join(timeout=15)
        with self.lock:
            if self.run_id:
                self._persist()
                if note:
                    self.db.execute("UPDATE mm_runs SET status='stopped', stopped_ts=?, note=? WHERE id=? AND status='running'",
                                    (self.clock(), note, self.run_id))
            self.state["running"] = False

    def shutdown(self):
        """App exit: keep the run marked running so it resumes after the restart."""
        self.stop(note=None)

    def _spawn(self):
        self._stop.clear()
        self.thread = threading.Thread(target=self.loop, daemon=True, name="mm-shadow")
        self.thread.start()

    def _load(self, rid, resumed=False):
        run = self.db.query("SELECT * FROM mm_runs WHERE id=?", (rid,))[0]
        self.run_id, self.p = rid, json.loads(run["params"])
        self.markets = {r["ticker"]: json.loads(r["info"]) for r in self.db.query("SELECT * FROM mm_markets WHERE run_id=?", (rid,))}
        st = {(r["ticker"], r["model"]): json.loads(r["state"]) for r in self.db.query("SELECT * FROM mm_state WHERE run_id=?", (rid,))}
        now = self.clock()
        self.sims, self.cursor = {}, {}
        for t, info in self.markets.items():
            for m in MODELS:
                s = st.get((t, m))
                if s is not None and resumed:
                    s["quotes"], s["last_ts"], s["mid_hist"] = {}, None, []
                    self.cursor[t] = s.pop("_cursor", None) or {"ts": now, "seen": []}
                self.sims[(t, m)] = SimQuoter(info, self.p, m, s)
            if t not in self.cursor:
                self.cursor[t] = {"ts": now, "seen": []}
            if resumed:
                self.cursor[t] = {"ts": now, "seen": []}     # trades while we were down never met a live quote
        self.mkt_ts = 0.0
        self.state.update(running=True, polls=0, errors=0, last_error=None)

    # ---- loop ------------------------------------------------------------------
    def loop(self):
        poll = float(self.p.get("poll_sec", 10))
        while not self._stop.is_set():
            t0 = time.time()
            try:
                with self.lock:
                    run = self.db.query("SELECT ts FROM mm_runs WHERE id=?", (self.run_id,))
                    if run and self.clock() - run[0]["ts"] > float(self.p.get("max_days", 21)) * 86400:
                        self._persist()
                        self.db.execute("UPDATE mm_runs SET status='done', stopped_ts=?, note='max_days reached' WHERE id=?",
                                        (self.clock(), self.run_id))
                        self.state["running"] = False
                        return
                    self.step()
            except Exception as e:
                self.state["errors"] += 1
                self.state["last_error"] = f"{time.strftime('%H:%M:%S')} {e}"[:300]
            self._stop.wait(max(1.0, poll - (time.time() - t0)))
        self.state["running"] = False

    def _trades(self, t: str) -> list[dict]:
        c = self.cursor[t]
        out, seen = [], set(c["seen"])
        params = {"ticker": t, "min_ts": int(c["ts"]) - 1, "limit": 1000}
        for tr in self.client.paginate("/markets/trades", "trades", params, max_items=5000):
            tid = tr.get("trade_id")
            if not tid or tid in seen:
                continue
            seen.add(tid)
            ts = iso_ts(tr.get("created_time")) or 0.0
            out.append({"id": tid, "ts": ts, "yes_price": fnum(tr.get("yes_price_dollars")) or None,
                        "count": fnum(tr.get("count_fp") or tr.get("count")), "taker_book_side": tr.get("taker_book_side")})
        self.state["requests"] += 1
        out.sort(key=lambda x: x["ts"])
        if out:
            c["ts"] = max(c["ts"], out[-1]["ts"])
            c["seen"] = (c["seen"] + [x["id"] for x in out])[-300:]
        return out

    def step(self):
        now = self.clock()
        tickers = sorted(self.markets)
        if now - self.mkt_ts > 300 or not self.mkt:
            for m in self.client.markets(status=None, tickers=tickers, exclude_combos=False):
                self.mkt[m["ticker"]] = m
            self.mkt_ts = now
            self.state["requests"] += 1
        books = {}
        for i in range(0, len(tickers), 20):
            books.update(self.client.orderbooks(tickers[i:i + 20]))
            self.state["requests"] += 1
        max_dt = 3 * float(self.p.get("poll_sec", 10))
        fills = []
        mids = []
        for t in tickers:
            m = self.mkt.get(t) or {}
            ob = books.get(t) or {}
            yes_lv, no_lv = book_levels(ob.get("yes_dollars")), book_levels(ob.get("no_dollars"))
            yb = yes_lv[0][0] if yes_lv else None
            nb = no_lv[0][0] if no_lv else None
            mid = (yb + 1 - nb) / 2 if yb and nb else None
            open_ = m.get("status") == "active"
            res = m.get("result")
            sims = [self.sims[(t, md)] for md in MODELS]
            trades = self._trades(t) if any(s.s["quotes"] for s in sims) else self._skip_trades(t, now)
            prev_mid = self.last_mid.get(t, mid)
            for md, sim in zip(MODELS, sims):
                for f in sim.apply_trades(trades):
                    fills.append((self.run_id, t, md, f["ts"], f["side"], f["price"], f["count"], f["fee"], prev_mid, f["kind"]))
                sim.accrue(now, yes_lv, no_lv, open_, max_dt)
                if res in ("yes", "no"):
                    sim.settle(res)
                sim.requote(now, m, yes_lv, no_lv)
            if mid is not None:
                self.last_mid[t] = mid
                w = self.mid_written.get(t)
                if not w or abs(w[1] - mid) > EPS or now - w[0] >= 300:
                    mids.append((self.run_id, t, now, mid, yb, 1 - nb))
                    self.mid_written[t] = (now, mid)
        with self.db.lock:
            if fills:
                self.db.conn.executemany("INSERT INTO mm_fills VALUES(?,?,?,?,?,?,?,?,?,?)", fills)
            if mids:
                self.db.conn.executemany("INSERT INTO mm_mids VALUES(?,?,?,?,?,?)", mids)
            self.db.conn.commit()
        self.state["polls"] += 1
        self.state["last_poll"] = now
        if now - self.persist_ts >= 60:
            self._persist()

    def _skip_trades(self, t, now) -> list:
        """No quote was resting, so no trade could fill us: just move the cursor (saves a request)."""
        self.cursor[t] = {"ts": now, "seen": []}
        return []

    def _persist(self):
        now = self.clock()
        rows = []
        for (t, m), sim in self.sims.items():
            s = dict(sim.s)
            s["_cursor"] = self.cursor.get(t)
            s["_mid"] = self.last_mid.get(t)
            rows.append((self.run_id, t, m, json.dumps(s), now))
        with self.db.lock:
            self.db.conn.executemany("INSERT OR REPLACE INTO mm_state VALUES(?,?,?,?,?)", rows)
            self.db.conn.commit()
        self.persist_ts = now

    # ---- report ------------------------------------------------------------------
    def status(self) -> dict:
        return {**self.state, "run_id": self.run_id, "markets": len(self.markets),
                "alive": bool(self.thread and self.thread.is_alive())}


def report(wdb, run_id: int | None = None, live: Shadow | None = None, now: float | None = None) -> dict | None:
    ensure_schema(wdb)
    if run_id is None:
        r = wdb.query("SELECT id FROM mm_runs ORDER BY id DESC LIMIT 1")
        if not r:
            return None
        run_id = r[0]["id"]
    run = wdb.query("SELECT * FROM mm_runs WHERE id=?", (run_id,))
    if not run:
        return None
    run = run[0]
    now = now or time.time()
    if live and live.run_id == run_id:
        with live.lock:
            states = {(t, m): json.loads(json.dumps(sim.s)) for (t, m), sim in live.sims.items()}
            mids_now = dict(live.last_mid)
    else:
        states, mids_now = {}, {}
        for r in wdb.query("SELECT * FROM mm_state WHERE run_id=?", (run_id,)):
            s = json.loads(r["state"])
            states[(r["ticker"], r["model"])] = s
            if s.get("_mid") is not None:
                mids_now[r["ticker"]] = s["_mid"]
    markets = {r["ticker"]: json.loads(r["info"]) for r in wdb.query("SELECT * FROM mm_markets WHERE run_id=?", (run_id,))}
    end = run["stopped_ts"] or now
    days = max(1e-9, (end - run["ts"]) / 86400)
    # mid history for markouts
    hist: dict[str, tuple[list, list]] = {}
    for r in wdb.query("SELECT ticker, ts, mid FROM mm_mids WHERE run_id=? ORDER BY ts", (run_id,)):
        h = hist.setdefault(r["ticker"], ([], []))
        h[0].append(r["ts"])
        h[1].append(r["mid"])

    def mid_at(t, ts):
        h = hist.get(t)
        if not h:
            return None
        i = bisect.bisect_right(h[0], ts) - 1
        return h[1][i] if i >= 0 else None

    fills = wdb.query("SELECT * FROM mm_fills WHERE run_id=? ORDER BY ts", (run_id,))
    out_models = {}
    for md in MODELS:
        tot = {k: 0.0 for k in ("reward", "cost", "fees", "value", "contracts", "fills", "total_sec", "elig_sec", "quoted_sec", "jumps")}
        per = []
        for t, info in markets.items():
            s = states.get((t, md))
            if not s:
                continue
            sim = SimQuoter(info, {}, md, s)
            val = sim.value(mids_now.get(t))
            trading = val - s["cost"] - s["fees"]
            row = {"ticker": t, "title": info.get("title"), "sub": info.get("sub"), "pool_daily": info["daily"],
                   "scan_est_daily": info.get("scan_est_daily"), "reward": round(s["reward"], 4),
                   "fills": s["fills"], "contracts": s["contracts"], "pos_yes": s["pos_yes"], "pos_no": s["pos_no"],
                   "cost": round(s["cost"], 2), "fees": round(s["fees"], 2), "value": round(val, 2),
                   "trading_pnl": round(trading, 2), "net": round(s["reward"] + trading, 2),
                   "quoted_pct": round(100 * s["quoted_sec"] / s["total_sec"], 1) if s["total_sec"] else None,
                   "eligible_pct": round(100 * s["elig_sec"] / s["total_sec"], 1) if s["total_sec"] else None,
                   "jumps": s.get("jumps", 0), "settled": s.get("settled"), "max_capital": round(s.get("max_capital", 0.0), 2)}
            per.append(row)
            for k in ("reward", "cost", "fees", "contracts", "fills", "total_sec", "elig_sec", "quoted_sec", "jumps"):
                tot[k] += s.get(k, 0.0)
            tot["value"] += val
        mk_sum = {h: [0.0, 0.0] for h in MARKOUTS}
        edge_sum = [0.0, 0.0]
        for f in (x for x in fills if x["model"] == md):
            sign_px = f["price"]
            if f["mid"] is not None:
                fair = f["mid"] if f["side"] == "yes" else 1 - f["mid"]
                edge_sum[0] += (fair - sign_px) * f["count"]
                edge_sum[1] += f["count"]
            for h in MARKOUTS:
                if now < f["ts"] + h and not run["stopped_ts"]:
                    continue
                m = mid_at(f["ticker"], f["ts"] + h)
                if m is None:
                    continue
                fair = m if f["side"] == "yes" else 1 - m
                mk_sum[h][0] += (fair - sign_px) * f["count"]
                mk_sum[h][1] += f["count"]
        trading = tot["value"] - tot["cost"] - tot["fees"]
        per.sort(key=lambda r: -r["net"])
        out_models[md] = {
            "reward": round(tot["reward"], 2), "fills": int(tot["fills"]), "contracts": round(tot["contracts"], 2),
            "cost": round(tot["cost"], 2), "fees": round(tot["fees"], 2), "value": round(tot["value"], 2),
            "trading_pnl": round(trading, 2), "net": round(tot["reward"] + trading, 2),
            "reward_per_day": round(tot["reward"] / days, 2), "net_per_day": round((tot["reward"] + trading) / days, 2),
            "edge_at_fill_cents": round(100 * edge_sum[0] / edge_sum[1], 2) if edge_sum[1] else None,
            "markout_cents": {f"{h // 60}m": (round(100 * v[0] / v[1], 2) if v[1] else None) for h, v in mk_sum.items()},
            "quoted_pct": round(100 * tot["quoted_sec"] / tot["total_sec"], 1) if tot["total_sec"] else None,
            "eligible_pct": round(100 * tot["elig_sec"] / tot["total_sec"], 1) if tot["total_sec"] else None,
            "jumps": int(tot["jumps"]), "max_capital_sum": round(sum(r["max_capital"] for r in per), 2),
            "markets": per,
        }
    return {"run_id": run_id, "started": run["ts"], "stopped": run["stopped_ts"], "status": run["status"], "note": run["note"],
            "days": round(days, 3), "params": json.loads(run["params"]), "markets": len(markets),
            "scan_est_daily": round(sum(m.get("scan_est_daily") or 0 for m in markets.values()), 2),
            "models": out_models}
