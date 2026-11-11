"""Phase 2.1 tests: weather data parsing, settlement mapping, no-lookahead fair values, scorecard, backtest strategy,
collector (mocked IEM), API endpoints."""
import datetime as dt
import json
import math
import random
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import weather as W  # noqa: E402
from app.backtest import Backtester  # noqa: E402
from app.warehouse import WarehouseDB  # noqa: E402

OK, FAIL = [], []
H = 3600


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def utc(y, m, d, h=0):
    return dt.datetime(y, m, d, h, tzinfo=dt.timezone.utc).timestamp()


NBS_HEADER = ("station,model,runtime,ftime,n_x,tmp,dpt,cld,wdr,wsp,p06,p12,q06,q12,t06,t12,snw,cig,vis,obv,poz,pos,typ,"
              "sky,swh,lcb,i06,slv,s06,pra,ppl,psn,pzr,t03,gst,q24,p24,t24,ccg,ppo,pco,lp1,lc1,cp1,cc1,s12,i12,s24,pzp,prs,txn")


def nbs_line(st, rt, ft, tmp, txn=""):
    f = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
    cols = [st, "NBS", f(rt), f(ft), "", str(tmp)] + [""] * 44 + [str(txn)]
    return ",".join(cols)


# ------------------------------------------------------------ synthetic world --
# Chicago Midway (CST, UTC-6). Daily true high/low; obs follow a diurnal curve; CLI = rounded true extremes
# (obs peak shaved by 0.6F so the CLI-vs-obs gap is learnable); NBS forecast = truth + per-day error.
OFF = -6
D0 = dt.date(2026, 7, 1)
NDAYS = 100
rng = random.Random(7)
TRUE = {}
for i in range(NDAYS + 3):
    d = D0 + dt.timedelta(days=i)
    hi = 75 + 10 * math.sin(i / 9) + rng.gauss(0, 3)
    lo = hi - 15 - rng.gauss(0, 2)
    TRUE[d] = (hi, lo, rng.gauss(0, 2.0), rng.gauss(0, 2.0))   # hi, lo, forecast err hi, forecast err lo


def temp_at(ts, err=(0.0, 0.0)):
    """Diurnal curve: min at 06 LST, max at 15 LST (LST = UTC-6)."""
    lst = dt.datetime.fromtimestamp(ts + OFF * 3600, dt.timezone.utc)
    d = lst.date()
    hi, lo, eh, el = TRUE.get(d, TRUE[D0])
    hi, lo = hi + err[0] * (eh if err[0] else 0), lo + err[1] * (el if err[1] else 0)
    h = lst.hour + lst.minute / 60
    if 6 <= h <= 15:
        return lo + (hi - lo) * (1 - math.cos(math.pi * (h - 6) / 9)) / 2
    nh, nl = (TRUE.get(d + dt.timedelta(days=1), TRUE[D0]) if h > 15 else (hi, lo))[:2]
    if h > 15:
        return hi - (hi - nl) * (1 - math.cos(math.pi * (h - 15) / 15)) / 2
    pl = TRUE.get(d - dt.timedelta(days=1), TRUE[D0])[0]
    return pl - (pl - lo) * (1 - math.cos(math.pi * (h + 9) / 15)) / 2


def build_world(db: WarehouseDB, ndays=NDAYS, with_markets=True):
    store = W.WxStore(db)
    s0 = utc(D0.year, D0.month, D0.day) - 2 * 86400
    rows_nbs, rows_obs, rows_cli = [], [], []
    for rt in W.WxCollector.runtimes(s0, s0 + (ndays + 2) * 86400):
        for k in range(1, 33):
            ft = rt + k * 3 * H
            lst_d = dt.datetime.fromtimestamp(ft + OFF * 3600, dt.timezone.utc).date()
            _, _, eh, el = TRUE.get(lst_d, TRUE[D0])
            tmp = round(temp_at(ft) + 0.5 * (eh + el), 0)
            hr = dt.datetime.fromtimestamp(ft, dt.timezone.utc).hour
            txn = None
            if hr == 0:     # daytime max of the LST day that just ended its afternoon
                dd = dt.datetime.fromtimestamp(ft - 6 * H + OFF * 3600, dt.timezone.utc).date()
                txn = round(TRUE.get(dd, TRUE[D0])[0] + TRUE.get(dd, TRUE[D0])[2])
            if hr == 12:
                dd = dt.datetime.fromtimestamp(ft + OFF * 3600, dt.timezone.utc).date()
                txn = round(TRUE.get(dd, TRUE[D0])[1] + TRUE.get(dd, TRUE[D0])[3])
            rows_nbs.append(("KMDW", rt, ft, tmp, txn))
    t = s0
    while t < s0 + (ndays + 2) * 86400:
        rows_obs.append(("MDW", int(t), round(temp_at(t) - (0.6 if True else 0), 1)))
        t += 300
    for i in range(ndays):
        d = D0 + dt.timedelta(days=i)
        hi, lo = TRUE[d][:2]
        # CLI extremes from the unshaved curve sampled every minute
        ds, de = W.day_window(d, OFF)
        vals = [temp_at(x) for x in range(int(ds), int(de), 60)]
        rows_cli.append(("KMDW", d.isoformat(), round(max(vals)), round(min(vals)), f"P{d}"))
    store.add_nbs(rows_nbs)
    store.add_obs(rows_obs)
    store.add_cli(rows_cli)
    if with_markets:
        add_markets(db, rows_cli)
    return store


def add_markets(db, rows_cli):
    """6 buckets per day for highs and lows, results from the CLI value, flat 50c market candles + trades."""
    cli = {r[1]: (r[2], r[3]) for r in rows_cli}
    for i in range(NDAYS):
        d = D0 + dt.timedelta(days=i)
        code = d.strftime("%y") + d.strftime("%b").upper() + d.strftime("%d")
        ds, de = W.day_window(d, OFF)
        for kind, series, word, val in (("high", "KXHIGHCHI", "maximum", cli[d.isoformat()][0]),
                                        ("low", "KXLOWTCHI", "minimum", cli[d.isoformat()][1])):
            ev = f"{series}-{code}"
            c = round(TRUE[d][0] if kind == "high" else TRUE[d][1])
            buckets = [("less", None, c - 4)] + [("between", c - 4 + 2 * j, c - 3 + 2 * j) for j in range(4)] + [("greater", c + 3, None)]
            for st, fl, cp in buckets:
                tk = f"{ev}-{st[0].upper()}{fl if fl is not None else cp}"
                res = W.bucket_yes_prob({int(val): 1.0}, st, fl, cp)
                raw = {"rules_primary": f"If the {word} temperature recorded at Chicago (CLIMDW) for X is ... according to The Weather Company",
                       "custom_strike": {"City": "Chicago"}}
                close = de + 1 * H
                db.execute("INSERT OR REPLACE INTO wh_markets(ticker,event_ticker,series_ticker,category,title,sub_title,market_type,status,"
                           "open_ts,close_ts,result,strike_type,floor_strike,cap_strike,raw,complete,volume) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
                           (tk, ev, series, "Climate and Weather", tk, f"{fl}-{cp}", "binary", "finalized", ds - 40 * H, close,
                            "yes" if res > 0.5 else "no", st, fl, cp, json.dumps(raw), 1000))
                rows = []
                for h in range(0, 42):
                    end = int(ds - 40 * H + (h + 1) * H)
                    rows.append((tk, 60, end, 0.48, None, None, 0.48, 0.52, None, None, 0.52, None, None, None, 0.5, None, 10, 0))
                db.executemany("INSERT OR REPLACE INTO wh_candles VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                trs = []
                for h in range(0, 42, 2):
                    ts = ds - 40 * H + h * H + 1800
                    trs.append((f"{tk}-t{h}", tk, ts, 0.40, 50, "no", "bid", 0))
                    trs.append((f"{tk}-u{h}", tk, ts + 60, 0.60, 50, "yes", "ask", 0))
                db.executemany("INSERT OR REPLACE INTO wh_trades VALUES(?,?,?,?,?,?,?,?)", trs)


def main():
    # ---------------------------------------------------------------- pure helpers
    check("parse rules: max/min + station", W.parse_rules("If the maximum temperature recorded at Chicago (CLIMDW) for Nov 9") == ("high", "MDW")
          and W.parse_rules("If the minimum temperature recorded at New York City (CLINYC) for Nov 11") == ("low", "NYC")
          and W.parse_rules("Will it rain") is None)
    check("event date from ticker", W.event_date("KXHIGHCHI-26NOV09") == dt.date(2026, 11, 9) and W.event_date("X-26FOO01") is None)
    a, b = W.day_window(dt.date(2026, 11, 11), -6)
    check("climate day = local STANDARD time (Chicago 06Z-06Z)", a == utc(2026, 11, 11, 6) and b == utc(2026, 11, 12, 6))
    a, b = W.day_window(dt.date(2026, 7, 4), -5)
    check("standard time even in summer (NYC 05Z-05Z, i.e. 1am-1am EDT)", a == utc(2026, 7, 4, 5) and b == utc(2026, 7, 5, 5))
    dist = {60: 0.1, 61: 0.2, 62: 0.3, 63: 0.4}
    check("bucket 'greater' floor=62 -> T>62 (63 or above)", abs(W.bucket_yes_prob(dist, "greater", 62, None) - 0.4) < 1e-9)
    check("bucket 'less' cap=61 -> T<61 (60 or below)", abs(W.bucket_yes_prob(dist, "less", None, 61) - 0.1) < 1e-9)
    check("bucket 'between' 61-62 inclusive", abs(W.bucket_yes_prob(dist, "between", 61, 62) - 0.5) < 1e-9)
    check("unknown strike type -> None", W.bucket_yes_prob(dist, "custom", 1, 2) is None)
    check("bucket of hours left", W.bucket_of(2) == 0 and W.bucket_of(5) == 1 and W.bucket_of(23) == 4 and W.bucket_of(59) == 6 and W.bucket_of(61) is None)
    rts = W.WxCollector.runtimes(utc(2026, 5, 4), utc(2026, 5, 6) - 1)
    check("NBS runtimes switch 1/7/13/19Z -> 0/6/12/18Z on 2026-05-05",
          [dt.datetime.fromtimestamp(t, dt.timezone.utc).hour for t in rts] == [1, 7, 13, 19, 0, 6, 12, 18])
    txt = NBS_HEADER + "\n" + nbs_line("KMDW", utc(2026, 11, 11, 6), utc(2026, 11, 11, 12), 44) + "\n" + \
        nbs_line("KMDW", utc(2026, 11, 11, 6), utc(2026, 11, 12, 0), 50, "54.0") + "\n" + \
        nbs_line("KMDW", utc(2026, 11, 11, 6), utc(2026, 11, 20, 0), 50, "54.0")
    rows = W.WxCollector.parse_nbs(txt)
    check("parse NBS csv (tmp, txn, horizon cut)", rows == [("KMDW", int(utc(2026, 11, 11, 6)), int(utc(2026, 11, 11, 12)), 44.0, None),
                                                          ("KMDW", int(utc(2026, 11, 11, 6)), int(utc(2026, 11, 12, 0)), 50.0, 54.0)], rows)
    obs = W.WxCollector.parse_obs("station,valid,tmpf\nMDW,2026-11-11 12:00,44.10\nMDW,2026-11-11 12:05,M\nMDW,2026-11-11 12:10,45.0\n", "MDW")
    check("parse ASOS csv (skips missing)", obs == [("MDW", int(utc(2026, 11, 11, 12)), 44.1), ("MDW", int(utc(2026, 11, 11, 12)) + 600, 45.0)], obs)
    cli = W.WxCollector.parse_cli({"results": [{"valid": "2026-11-10", "high": 66, "low": 42, "product": "p"},
                                               {"valid": "2026-11-11", "high": "M", "low": "M"}]}, "KMDW")
    check("parse CLI json (skips missing)", cli == [("KMDW", "2026-11-10", 66.0, 42.0, "p")], cli)

    # ---------------------------------------------------------------- synthetic world
    tmp = tempfile.mkdtemp()
    db = WarehouseDB(Path(tmp) / "warehouse.db")
    store = build_world(db)
    sm = store.series_map()
    check("series map from market rules", sm.get("KXHIGHCHI", {}).get("cli") == "MDW" and sm["KXLOWTCHI"]["kind"] == "low"
          and sm["KXHIGHCHI"]["offset"] == -6 and sm["KXHIGHCHI"]["mapped"], sm)

    db.execute("INSERT INTO wh_markets(ticker,event_ticker,series_ticker,close_ts,raw) VALUES(?,?,?,?,?)",
               ("KXHIGHCHI-OLD", "KXHIGHCHI-26JAN01", "KXHIGHCHI", 1.0,
                json.dumps({"rules_primary": "If the highest temperature recorded at Chicago Midway, IL is above 40"})))
    check("old rule wording on some markets doesn't hide the series", store.series_map().get("KXHIGHCHI", {}).get("cli") == "MDW")
    db.execute("DELETE FROM wh_markets WHERE ticker='KXHIGHCHI-OLD'")
    model = W.WxModel(store)
    d = D0 + dt.timedelta(days=70)
    ds, de = W.day_window(d, OFF)
    t = de - 20 * H
    mk = {"series_ticker": "KXHIGHCHI", "event_ticker": f"KXHIGHCHI-{d.strftime('%y%b%d').upper()}",
          "strike_type": "between", "floor_strike": round(TRUE[d][0]), "cap_strike": round(TRUE[d][0]) + 1}
    fv = model.fair(mk, t)
    check("fair value produced with enough history", fv is not None and 0 < fv["p_yes"] < 1, fv and {k: fv[k] for k in fv if k != "dist"})
    check("distribution sums to 1", fv is not None and abs(sum(fv["dist"].values()) - 1) < 1e-6)
    rt, _ = model.latest_run("KMDW", t)
    check("uses only runs published by t (runtime + 3h lag <= t)", rt is not None and rt + 3 * H <= t and rt + 9 * H > t, (rt, t))
    # forecast feature: rest-of-day extreme from the latest run, incl. the 00Z 12-h max / 12Z 12-h min
    tf = ds - 4 * H
    rtf, rowsf = model.latest_run("KMDW", tf)
    in_day = [r for r in rowsf if ds <= r[0] < de + 1]
    exp_hi = max([r[1] for r in in_day] + [r[2] for r in rowsf if r[2] is not None and
                                          dt.datetime.fromtimestamp(r[0], dt.timezone.utc).hour == 0 and ds <= r[0] - 6 * H < de])
    exp_lo = min([r[1] for r in in_day] + [r[2] for r in rowsf if r[2] is not None and
                                          dt.datetime.fromtimestamp(r[0], dt.timezone.utc).hour == 12 and ds + 2 * H <= r[0] <= de + 3 * H])
    xh, _ = model.forecast_x("MDW", "high", d, tf)
    xl, _ = model.forecast_x("MDW", "low", d, tf)
    check("forecast high = max(rest-of-day temps, daytime 12-h max)", xh == exp_hi and xh >= round(TRUE[d][0] + TRUE[d][2]) - 0.01, (xh, exp_hi))
    check("forecast low = min(rest-of-day temps, overnight 12-h min)", xl == exp_lo and xl <= round(TRUE[d][1] + TRUE[d][3]) + 0.01, (xl, exp_lo))
    check("forecast high > forecast low", xh > xl + 5)
    # 12-h max/min rows matter even when the 3-hourly temps miss the peak/trough
    dbx = WarehouseDB(Path(tmp) / "x.db")
    sx = W.WxStore(dbx)
    dd = dt.date(2026, 11, 20)
    a0, b0 = W.day_window(dd, -6)
    rt0 = int(utc(2026, 11, 20, 0))
    sx.add_nbs([("KMDW", rt0, rt0 + k * 3 * H, 50.0, None) for k in range(1, 16)] +
               [("KMDW", rt0, int(utc(2026, 11, 21, 0)), 50.0, 70.0), ("KMDW", rt0, int(utc(2026, 11, 20, 12)), 50.0, 30.0),
                ("KMDW", rt0, int(utc(2026, 11, 21, 12)), 50.0, 20.0)])
    mx = W.WxModel(sx)
    check("12-h max (00Z row) counts for the high", mx.forecast_x("MDW", "high", dd, rt0 + 4 * H)[0] == 70.0)
    check("12-h min (12Z row) counts for the low; next morning's does not", mx.forecast_x("MDW", "low", dd, rt0 + 4 * H)[0] == 30.0)
    check("12-h max ignored once its period is mostly past", mx.forecast_x("MDW", "high", dd, int(utc(2026, 11, 20, 20)))[0] == 50.0)
    # lookahead guards: future forecast run / future CLI / future obs must not change the answer
    before = json.dumps(model.distribution("MDW", "high", d, t), sort_keys=True, default=str)
    store.add_nbs([("KMDW", int(t - 1 * H), int(t + 3 * H), 140.0, None), ("KMDW", int(t - 1 * H), int(de - 6 * H), 140.0, 140.0)])
    store.add_cli([("KMDW", (d - dt.timedelta(days=0)).isoformat(), 140.0, 120.0, "x")])
    store.add_obs([("MDW", int(t + 600), 140.0)])
    after = json.dumps(W.WxModel(store).distribution("MDW", "high", d, t), sort_keys=True, default=str)
    check("no lookahead: a run published after t, today's CLI and later obs change nothing", before == after)
    store.db.execute("DELETE FROM wx_nbs WHERE tmp=140.0 OR txn=140.0")
    store.db.execute("DELETE FROM wx_obs WHERE tmpf=140.0")
    store.add_cli([("KMDW", d.isoformat(), round(max(temp_at(x) for x in range(int(ds), int(de), 60))),
                    round(min(temp_at(x) for x in range(int(ds), int(de), 60))), f"P{d}")])
    model = W.WxModel(store)
    # calibration window excludes days whose CLI wasn't out yet
    pooled, mine, gaps = model.calibration("high", 4, "MDW", t)
    check("calibration uses prior days only (>= 30 samples, <= 61 days)", 30 <= len(pooled) <= 61 and len(gaps) >= 10, (len(pooled), len(gaps)))
    check("learned CLI-vs-obs gap (obs shaved 0.6F -> CLI mostly +1)", sum(gaps) / len(gaps) > 0.3, sum(gaps) / max(1, len(gaps)))
    # after the day: concentrated on the observed extreme + gap
    late = model.distribution("MDW", "high", d, de + 30 * 60)
    top = max(late["dist"], key=late["dist"].get) if late else None
    cli_v = store.db.query("SELECT high FROM wx_cli WHERE station='KMDW' AND date=?", (d.isoformat(),))[0]["high"]
    check("after day end the distribution centres on the observed high (+gap)", late and abs(top - cli_v) <= 1 and late["dist"][top] > 0.5,
          late and (top, cli_v, late["dist"]))
    # late in the day with the max already observed, lower buckets collapse
    lt = model.distribution("MDW", "high", d, de - 2 * H)
    m_obs = lt["obs_so_far"]
    check("observed max so far truncates the distribution", lt and min(lt["dist"]) >= round(m_obs) - 3 and
          sum(p for k, p in lt["dist"].items() if k < round(m_obs)) < 0.2, lt and (m_obs, lt["dist"]))
    check("too little history -> no price", W.WxModel(store).distribution("MDW", "high", D0 + dt.timedelta(days=3), W.day_window(D0 + dt.timedelta(days=3), OFF)[1] - 20 * H) is None)
    check("unmapped series -> None", model.fair({**mk, "series_ticker": "KXNOPE"}, t) is None)
    # accuracy: model beats a flat 50c market and is calibrated-ish
    ev = W.evaluate(db, leads_h=(24, 6))
    sc = ev["settlement_check"]
    check("scorecard settlement check = 100% on synthetic data", sc["agree_pct"] == 100.0 and sc["cli_disagrees"] == 0, sc)
    r24, r6 = ev["by_hours_before_day_end"]["24"], ev["by_hours_before_day_end"]["6"]
    check("scorecard: model Brier < flat-market Brier at 24h and 6h", r24["n"] > 100 and r24["brier_model"] < r24["brier_market"]
          and r6["brier_model"] < r6["brier_market"], (r24, r6))
    check("scorecard: closer to the end = more accurate", r6["brier_model"] <= r24["brier_model"] + 1e-9, (r6["brier_model"], r24["brier_model"]))
    cal = r24["calibration"]
    hi_b = [v for k, v in cal.items() if float(k) >= 0.7 and v["n"] >= 20]
    lo_b = [v for k, v in cal.items() if float(k) <= 0.1 and v["n"] >= 20]
    check("scorecard: calibration buckets roughly honest", all(v["hit"] > 0.45 for v in hi_b) and all(v["hit"] < 0.2 for v in lo_b), cal)
    # mapping mismatch is caught
    db.execute("UPDATE wh_markets SET result = CASE result WHEN 'yes' THEN 'no' ELSE 'yes' END WHERE ticker IN "
               "(SELECT ticker FROM wh_markets WHERE series_ticker='KXHIGHCHI' LIMIT 3)")
    ev2 = W.evaluate(db, leads_h=(6,))
    check("scorecard flags buckets where the CLI disagrees with Kalshi", ev2["settlement_check"]["cli_disagrees"] == 3 and len(ev2["settlement_check"]["examples"]) == 3)
    db.execute("UPDATE wh_markets SET result = CASE result WHEN 'yes' THEN 'no' ELSE 'yes' END WHERE ticker IN "
               "(SELECT ticker FROM wh_markets WHERE series_ticker='KXHIGHCHI' LIMIT 3)")

    # ---------------------------------------------------------------- backtest strategy
    bt = Backtester(db)
    t0 = time.time()
    r = bt.run("weather_fair", {"edge": 0.05})
    dur = time.time() - t0
    th = r["models"]["through"]
    check("weather_fair backtest runs on weather series only", r["filters"]["series"] == ["KXHIGHCHI", "KXLOWTCHI"] and th["markets_simulated"] > 1000, r["filters"])
    check("weather_fair places maker orders only (no fees on quadratic)", th["fills"] > 0 and th["maker_fills"] == th["fills"] and th["fees"] == 0, th)
    check("weather_fair profits against a dumb 50c market (sanity)", th["pnl"] > 0, th["pnl"])
    check("backtest speed reasonable", dur < 240, dur)
    r2 = bt.run("weather_fair", {"edge": 0.6})
    check("edge too wide -> never fills", r2["models"]["through"]["fills"] == 0)
    r3 = bt.run("weather_fair", {"edge": 0.05, "max_position": 10, "size": 10})
    per = r3["models"]["touch"]["per_market"]
    check("position cap respected per market and side", all(x["contracts"] <= 20 for x in per), max((x["contracts"] for x in per), default=0))
    # bids never at or above fair - edge: recompute fair at fill placement is costly; check prices vs fair at fill ts
    m2 = W.WxModel(store)
    bad = 0
    mk_rows = {m["ticker"]: m for m in db.query("SELECT * FROM wh_markets")}
    for f in th["sample_fills"][:60]:
        m = mk_rows[f["ticker"]]
        # the order was placed at the last bar close before the fill
        place = (int(f["ts"]) // 3600) * 3600
        bars = db.query("SELECT end_ts FROM wh_candles WHERE ticker=? AND end_ts<=? ORDER BY end_ts DESC LIMIT 1", (f["ticker"], f["ts"]))
        place = bars[0]["end_ts"] if bars else place
        fv2 = m2.fair(m, place)
        if fv2:
            fair_side = fv2["p_yes"] if f["side"] == "yes" else 1 - fv2["p_yes"]
            if f["price"] > fair_side - 0.05 + 1e-9:
                bad += 1
    check("every sampled fill was bid at least `edge` below fair", bad == 0, bad)

    # ---------------------------------------------------------------- collector (mocked IEM)
    class Resp:
        def __init__(self, code, text="", js=None):
            self.status_code, self.text, self._js = code, text, js

        def json(self):
            return self._js

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

    class Sess:
        def __init__(self):
            self.headers, self.calls = {}, []

        def get(self, url, params=None, timeout=None):
            self.calls.append((url, params))
            p = dict(params) if isinstance(params, dict) else {}
            if isinstance(params, list):
                for k, v in params:
                    p.setdefault(k, v)
            if url.endswith("/api/1/mos.txt"):
                rt = p.get("runtime")
                if rt and rt.startswith("2026-11-05T06"):
                    return Resp(404, "nope")
                rts = W._ts(rt) if rt else int(utc(2026, 11, 11, 12))
                return Resp(200, NBS_HEADER + "\n" + nbs_line("KMDW", rts, rts + 6 * H, 50, ""))
            if url.endswith("/cgi-bin/request/asos.py"):
                self.asos = getattr(self, "asos", 0) + 1
                if self.asos in (1, 2) and getattr(self, "throttle", False):
                    return Resp(429, "Too many requests from your IP address, slow down.")
                return Resp(200, "station,valid,tmpf\nMDW,2026-11-10 12:00,44.0\n")
            if url.endswith("/json/cli.py"):
                return Resp(200, js={"results": [{"valid": "2026-11-10", "high": 66, "low": 42, "product": "p"}]})
            return Resp(500)

    db2 = WarehouseDB(Path(tmp) / "w2.db")
    raw = json.dumps({"rules_primary": "If the maximum temperature recorded at Chicago (CLIMDW) for x", "custom_strike": {"City": "Chicago"}})
    raw2 = json.dumps({"rules_primary": "If the maximum temperature recorded at Nowhere (CLIXYZ) for x"})
    now = utc(2026, 11, 11, 14)
    db2.execute("INSERT INTO wh_markets(ticker,event_ticker,series_ticker,close_ts,raw) VALUES(?,?,?,?,?)",
                ("KXHIGHCHI-26NOV10-B1", "KXHIGHCHI-26NOV10", "KXHIGHCHI", now - 86400, raw))
    db2.execute("INSERT INTO wh_markets(ticker,event_ticker,series_ticker,close_ts,raw) VALUES(?,?,?,?,?)",
                ("KXHIGHXYZ-26NOV10-B1", "KXHIGHXYZ-26NOV10", "KXHIGHXYZ", now - 86400, raw2))

    class E:
        class cfg:
            data = {"weather": {"request_gap_sec": 0, "backoff_sec": [0, 0]}}

        class db:
            @staticmethod
            def audit(*a):
                pass
    sess = Sess()
    sess.throttle = True
    col = W.WxCollector(E, W.WxStore(db2), session=sess)
    st = col.sync(now=now)
    cnt = W.WxStore(db2).counts()
    check("collector: NBS runs backfilled (404 run tolerated)", cnt["nbs_runs"] >= 20 and st["errors"] == 0, (cnt, st, col.state))
    check("collector: obs + CLI stored", cnt["obs_rows"] == 1 and cnt["cli_days"] == 1, cnt)
    check("collector: HTTP 429 -> backs off and retries", col.state.get("throttled") == 2 and sess.asos >= 3, (col.state.get("throttled"), sess.asos))
    check("collector: obs coverage marker advances only on success", db2.get("wx_obs_done_MDW") is not None)
    check("collector: unmapped station reported, not fetched", col.state["unmapped"] == ["XYZ"] and
          not any("XYZ" in json.dumps(c[1]) for c in sess.calls))
    check("collector: run first-seen time recorded (publish-lag check)", db2.query("SELECT count(*) n FROM wx_runs")[0]["n"] == 1)
    raw3 = json.dumps({"rules_primary": "If the minimum temperature recorded at Miami (CLIMIA) for x"})
    db2.execute("INSERT INTO wh_markets(ticker,event_ticker,series_ticker,close_ts,raw) VALUES(?,?,?,?,?)",
                ("KXLOWTMIA-26NOV10-B1", "KXLOWTMIA-26NOV10", "KXLOWTMIA", now - 86400, raw3))
    k0 = len(sess.calls)
    col.sync(now=now)
    mia = [c for c in sess.calls[k0:] if c[0].endswith("mos.txt") and ("station", "KMIA") in c[1]]
    check("collector: a newly mapped station gets the old runs backfilled", len(mia) >= 20, len(mia))
    n1 = len(sess.calls)
    col.sync(now=now)
    n_mos = sum(1 for c in sess.calls[n1:] if c[0].endswith("mos.txt"))
    check("collector: second sync doesn't refetch old runs", n_mos <= 4, n_mos)

    # ---------------------------------------------------------------- API
    import os
    os.environ["KALSHI_DESK_NO_WATCHDOG"] = "1"
    from fastapi.testclient import TestClient

    from app.config import Config
    from app.db import DB
    from app.server import create_app
    cfgp = Path(tmp) / "config.json"
    cfg = Config(cfgp)
    # point the app's warehouse at the synthetic world
    import shutil
    shutil.copy(Path(tmp) / "warehouse.db", Path(tmp) / "warehouse.db.bak")
    app = create_app(cfg, DB(Path(tmp) / "desk.db"), start_engine=False)
    c = TestClient(app)
    tok = c.get("/api/session").json()["token"]
    j = c.get("/api/weather").json()
    check("API /api/weather: status + series map", "collector" in j and j["series"]["KXHIGHCHI"]["cli"] == "MDW", list(j))
    b = c.get("/api/weather/board?days=2").json()
    check("API board responds (no open events in synthetic past world)", b["events"] == [] and "note" in b)
    check("API eval needs the CSRF token", c.post("/api/weather/eval").status_code in (401, 403))
    check("version 2.2.4", c.get("/api/session").status_code == 200 and __import__("app").__version__ == "2.2.4")
    check("weather collection never places orders", not app.state.engine.db.query("SELECT 1 FROM orders_log LIMIT 1"))
    del tok


if __name__ == "__main__":
    main()
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)
