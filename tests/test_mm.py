"""Phase 2.3 tests: Kalshi LIP scoring (hand-computed), landscape scan, shadow quoter fill models, report, API.
Nothing here may place an order."""
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["KALSHI_DESK_NO_WATCHDOG"] = "1"

from app import mm  # noqa: E402
from app.kalshi_client import KalshiClient  # noqa: E402
from app.warehouse import WarehouseDB  # noqa: E402
from mock_kalshi import create_mock  # noqa: E402

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def close(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def serve(app, port):
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    return srv


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


# ------------------------------------------------------------ scoring --
def scoring_tests():
    lv = mm.book_levels([["0.5000", "30"], ["0.4900", "50"], ["0.4800", "100"], ["0.4700", "500"], ["0.5000", "10"], ["0", "5"]])
    check("book levels merged by price, best first, junk dropped", lv == [(0.5, 40.0), (0.49, 50.0), (0.48, 100.0), (0.47, 500.0)], lv)
    # target 200: ref at cum >= 40 -> 0.50 level (40); walk stops at cum >= 200 -> 0.50,0.49,0.48 (190) then 0.47 (690)
    r = mm.side_score(lv, 200, 0.5)
    check("reference price = first level reaching target/5", close(r["ref"], 0.50), r)
    check("eligible when side holds >= target", r["eligible"] and close(r["total"], 690))
    # ours at 0.50 size 10: level 0.50 = 50 -> ref still 0.50 (cum 50 >= 40)
    # scores: .50: 50*1 ; .49: 50*.5 ; .48: 100*.25 ; .47: 500*.125  (walk reaches 200 at .47: 50+50+100=200 -> stops at .48!)
    r = mm.side_score(lv, 200, 0.5, (0.50, 10))
    tot = 50 + 50 * 0.5 + 100 * 0.25
    check("walk stops at the level where cumulative size reaches target (ours included)", close(r["share"], 10 / tot), (r, 10 / tot))
    r = mm.side_score(lv, 200, 0.5, (0.48, 10))
    tot = 40 + 50 * 0.5 + 110 * 0.25
    check("bid below reference discounted by df^ticks", close(r["share"], 10 * 0.25 / tot), (r["share"], 10 * 0.25 / tot))
    r = mm.side_score(lv, 200, 0.5, (0.46, 10))
    check("bid beyond the qualifying walk scores 0", close(r["share"], 0.0), r)
    r = mm.side_score(lv, 200, 1.0, (0.51, 10))
    check("bid above the reference price: N = 0", close(r["share"], 10 / (10 + 40 + 50 + 100)), r)
    thin = [(0.5, 30.0), (0.49, 20.0)]
    s = mm.snapshot_score(thin, lv, 200, 1.0, (0.5, 10), (0.5, 10))
    check("snapshot excluded when one side lacks target size", not s["eligible"] and s["score"] == 0, s)
    s = mm.snapshot_score(lv, lv, 200, 1.0, (0.5, 10), None)
    check("snapshot score = YES share + NO share (one side only)", s["eligible"] and close(s["score"], 10 / 200) and s["share_no"] == 0, s)
    now = 1_800_000_000
    progs = [
        {"market_ticker": "A", "incentive_type": "liquidity", "start_date": iso(now - 86400), "end_date": iso(now + 6 * 86400),
         "period_reward": 700000, "target_size_fp": "300.00", "discount_factor_bps": 5000},
        {"market_ticker": "A", "incentive_type": "liquidity", "start_date": iso(now - 3600), "end_date": iso(now + 2 * 86400),
         "period_reward": 100000, "target_size_fp": "100.00", "max_reward_per_account": 50000},
        {"market_ticker": "B", "incentive_type": "liquidity", "start_date": iso(now + 3600), "end_date": iso(now + 86400),
         "period_reward": 999999},
        {"market_ticker": "C", "incentive_type": "volume", "start_date": iso(now - 3600), "end_date": iso(now + 86400),
         "period_reward": 999999},
    ]
    pools = mm.aggregate_programs(progs, now)
    a = pools.get("A") or {}
    exp_daily = 70 / 7 + 10 / (2 + 1 / 24)
    check("programs: future and non-liquidity programs ignored", set(pools) == {"A"}, list(pools))
    check("programs: $/day summed across overlapping programs", close(a.get("daily"), exp_daily, 1e-6), (a.get("daily"), exp_daily))
    check("programs: max target, discount factor, per-account cap, days left",
          a["target"] == 300 and a["df"] == 0.5 and a["max_per_account"] == 5 and close(a["days_left"], 6), a)


# ----------------------------------------------------------- sim quoter --
P = {"size": 10, "max_inventory": 30, "pull_hours": 6, "jump_cents": 4, "cooldown_min": 10, "refresh_sec": 15,
     "poll_sec": 10, "min_mid": 0.15, "max_mid": 0.85, "max_spread": 0.06, "requeue_sec": 60}
INFO = {"ticker": "T", "daily": 86.4, "target": 100, "df": 1.0, "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}
MK = {"status": "active", "market_type": "binary", "close_time": "2030-01-01T00:00:00Z"}
YES = [(0.40, 100.0), (0.39, 200.0)]
NO = [(0.58, 50.0), (0.57, 200.0)]          # YES ask 0.42


def tr(ts, yp, n, side):
    return {"ts": ts, "yes_price": yp, "count": n, "taker_book_side": side}


def sim_tests():
    q = mm.SimQuoter(INFO, P, "queue")
    t = mm.SimQuoter(INFO, P, "touch")
    for s in (q, t):
        s.requote(1000, MK, YES, NO)
    check("quotes join best YES bid and best NO bid", q.s["quotes"]["yes"]["px"] == 0.40 and q.s["quotes"]["no"]["px"] == 0.58)
    check("queue starts behind the whole level", q.s["quotes"]["yes"]["ahead"] == 100 and q.s["quotes"]["no"]["ahead"] == 50)
    check("trades before placement never fill", not q.apply_trades([tr(999, 0.40, 500, "ask")]))
    f1 = q.apply_trades([tr(1001, 0.40, 60, "ask")])
    f2 = t.apply_trades([tr(1001, 0.40, 60, "ask")])
    check("queue: trade at your price eats the queue ahead first", not f1 and q.s["quotes"]["yes"]["ahead"] == 40, q.s["quotes"]["yes"])
    check("touch: trade at your price fills you at once", len(f2) == 1 and f2[0]["count"] == 10 and t.s["pos_yes"] == 10)
    check("touch: maker fee per the series' fee type (rounded up to the cent)", close(f2[0]["fee"], 0.05), f2)
    f = q.apply_trades([tr(1002, 0.40, 45, "ask")])
    check("queue: overflow past the queue fills you", len(f) == 1 and f[0]["count"] == 5 and q.s["quotes"]["yes"]["left"] == 5, f)
    check("trade at your price on the wrong side does not fill", not q.apply_trades([tr(1003, 0.40, 50, "bid")]) and q.s["quotes"]["yes"]["left"] == 5)
    f = q.apply_trades([tr(1004, 0.39, 1, "ask")])
    check("trade through your price fills the rest", len(f) == 1 and f[0]["count"] == 5 and f[0]["kind"] == "through" and q.s["pos_yes"] == 10, f)
    f = q.apply_trades([tr(1005, 0.43, 3, "bid")])
    check("NO bid 0.58 filled by a YES print above 0.42", len(f) == 1 and f[0]["side"] == "no" and f[0]["count"] == 10, f)
    check("position and cost tracked", q.s["pos_yes"] == 10 and q.s["pos_no"] == 10 and close(q.s["cost"], 10 * 0.40 + 10 * 0.58))
    check("YES + NO pair worth $1 whatever the mid", close(q.value(0.3), 10) and close(q.value(0.9), 10))
    # requote after fills: new orders at the back of the queue
    q.requote(1010, MK, YES, NO)
    check("filled quotes are re-placed at the back of the queue", q.s["quotes"]["yes"]["left"] == 10 and q.s["quotes"]["yes"]["ahead"] == 100 and q.s["quotes"]["yes"]["placed"] == 1010)
    # level shrinks below the queue ahead -> ahead capped
    q.requote(1020, MK, [(0.40, 30.0)] + YES[1:], NO)
    check("queue ahead capped by the size shown at the level", q.s["quotes"]["yes"]["ahead"] == 30 and q.s["quotes"]["yes"]["placed"] == 1010)
    q.requote(1075, MK, YES, NO)
    check("re-placed every requeue_sec (loses queue priority, like the live quoter)", q.s["quotes"]["yes"]["placed"] == 1075 and q.s["quotes"]["yes"]["ahead"] == 100)
    # inventory cap
    s = mm.SimQuoter(INFO, P, "touch")
    s.s["pos_yes"] = 30
    s.requote(2000, MK, YES, NO)
    check("inventory cap: stop bidding YES at +30, keep NO", "yes" not in s.s["quotes"] and "no" in s.s["quotes"])
    # jump + cooldown
    s = mm.SimQuoter(INFO, P, "touch")
    s.requote(3000, MK, YES, NO)
    s.requote(3010, MK, [(0.45, 100.0)], [(0.53, 100.0)])
    check("mid jump >= 4c pulls quotes and starts cool-down", not s.s["quotes"] and s.s["jumps"] == 1 and close(s.s["cooldown_until"], 3610), s.s)
    s.requote(3100, MK, [(0.45, 100.0)], [(0.53, 100.0)])
    check("no quotes during cool-down", not s.s["quotes"])
    s.requote(3611, MK, [(0.45, 100.0)], [(0.53, 100.0)])
    check("quotes resume after cool-down", "yes" in s.s["quotes"])
    # filters
    s = mm.SimQuoter(INFO, P, "touch")
    s.requote(4000, MK, [(0.30, 100.0)], [(0.60, 100.0)])
    check("no quotes when spread > max_spread", not s.s["quotes"])
    s.requote(4010, {**MK, "close_time": iso(4010 + 3 * 3600)}, YES, NO)
    check("no quotes within pull_hours of close", not s.s["quotes"])
    # rewards: $86.4/day = $0.001/s; YES level .40 = 100 others + 10 ours, NO .58 = 50 + 10
    s = mm.SimQuoter(INFO, P, "touch")
    s.requote(5000, MK, YES, NO)
    s.accrue(5000, YES, NO, True, 30)
    s.accrue(5010, YES, NO, True, 30)
    # target 100: YES walk .40 -> 110 >= 100 stop -> share 10/110 ; NO walk .58 (60) .57 (260) -> ref .58 (60 >= 20); share 10/(60+200)
    exp = 0.001 * 10 * (10 / 110 + 10 / 260) / 2
    check("reward accrual = pool/s x seconds x (YES share + NO share) / 2", close(s.s["reward"], exp, 1e-9), (s.s["reward"], exp))
    s.accrue(5100, YES, NO, True, 30)
    check("gaps longer than 3 polls are not credited", close(s.s["total_sec"], 40), s.s["total_sec"])
    s.accrue(5110, YES, [(0.58, 20.0)], True, 30)
    check("no reward for snapshots excluded by thin liquidity", close(s.s["elig_sec"], 40), s.s["elig_sec"])
    # defences
    d1 = mm.SimQuoter(INFO, {**P, "pull_after_fill_min": 10}, "touch")
    d1.requote(6000, MK, YES, NO)
    d1.apply_trades([tr(6001, 0.40, 5, "ask")])
    d1.requote(6010, MK, YES, NO)
    check("pull after fill: filled side stays out, other side keeps quoting", "yes" not in d1.s["quotes"] and "no" in d1.s["quotes"])
    d1.requote(6602, MK, YES, NO)
    check("pull after fill: side returns after the pause", "yes" in d1.s["quotes"])
    d2 = mm.SimQuoter(INFO, {**P, "offset_ticks": 1}, "queue")
    d2.requote(7000, MK, YES, NO)
    check("offset: 1 tick behind on both sides, queue = size at that level", d2.s["quotes"]["yes"]["px"] == 0.39 and d2.s["quotes"]["yes"]["ahead"] == 200
          and d2.s["quotes"]["no"]["px"] == 0.57)
    check("offset: a trade at the best bid does not fill you", not d2.apply_trades([tr(7001, 0.40, 500, "ask")]))
    d2.accrue(7000, YES, NO, True, 30)
    d2.accrue(7010, YES, NO, True, 30)
    # YES: walk reaches target 100 at .40, so our .39 bid doesn't qualify (0). NO: .58 (50) then .57 (200 + our 10)
    # -> ours qualifies, df 1.0 -> share 10/260
    exp = 0.001 * 10 * (0 + 10 / 260) / 2
    check("offset: scored per Kalshi's walk (YES behind the walk earns 0, NO qualifies)", close(d2.s["reward"], exp, 1e-12), (d2.s["reward"], exp))
    d3 = mm.SimQuoter(INFO, {**P, "exit_step_in": True}, "queue")
    d3.s["pos_yes"] = 30
    d3.requote(8000, MK, YES, NO)
    check("exit step-in: at the cap, only the reducing side quotes, 1c inside with nobody ahead",
          set(d3.s["quotes"]) == {"no"} and d3.s["quotes"]["no"]["px"] == 0.59 and d3.s["quotes"]["no"]["ahead"] == 0, d3.s["quotes"])
    d3.requote(8001, MK, YES, [(0.59, 50.0)])
    check("exit step-in: no step when the spread is 1c", d3.s["quotes"]["no"]["px"] == 0.59 and (1 - 0.59) - 0.40 < 0.02)
    d4 = mm.SimQuoter(INFO, {**P, "market_loss_stop": 1.0}, "touch")
    d4.s.update(pos_yes=20, cost=10.0)                       # bought 20 YES at .50; mid now .41 -> down $1.80
    d4.requote(9000, MK, YES, NO)
    check("per-market loss stop: exit-only (just the reducing side) once the market is down past the stop",
          set(d4.s["quotes"]) == {"no"} and d4.s["stops"] == 1 and d4.s["stop_until"] > 9000, d4.s["quotes"])
    d5 = mm.SimQuoter(INFO, P, "touch")
    d5.requote(9100, MK, YES, NO, allow_open=False)
    check("capital cap reached and flat: no new quotes", d5.s["quotes"] == {})
    d5.s.update(pos_no=10, cost=5.8)
    d5.requote(9200, MK, YES, NO, allow_open=False)
    check("capital cap reached with inventory: only the reducing side", set(d5.s["quotes"]) == {"yes"})
    check("tied-up capital = cost - $1 per YES/NO pair + open quotes", close(d5.tied_up(), 5.8 + 0.40 * 10))
    d5.s.update(pos_yes=10, cost=9.8)
    d5.s["quotes"] = {}
    check("a full YES+NO pair ties up nothing (it pays out $1)", close(d5.tied_up(), 0.0))
    d6 = mm.SimQuoter(INFO, P, "touch")
    d6.s.update(pos_yes=30, pos_no=10)
    check("liquidation value: pairs at $1, the rest at the bid for the side held", close(d6.liq_value(0.40, 0.42, 0.41), 10 + 20 * 0.40)
          and close(d6.value(0.41), 30 * 0.41 + 10 * 0.59))
    s.settle("no")
    check("settled market: quotes dropped, value = payout", not s.s["quotes"] and s.value(0.5) == s.s["pos_no"])


# ------------------------------------------------------- scan + shadow --
def integration_tests(tmp):
    port = free_port()
    mock = create_mock({})
    serve(mock, port)
    base = f"http://127.0.0.1:{port}/trade-api/v2"
    wdb = WarehouseDB(Path(tmp) / "wh.db")
    sc = mm.Scanner(wdb, client=KalshiClient(base), req_per_sec=50)
    sid = sc.db.execute("INSERT INTO mm_scans(ts,status,progress,params) VALUES(?, 'running', '', '{}')", (time.time(),)).lastrowid
    summ = sc.run(sid, {})
    check("scan: both programs found", summ["programs"] == 2 and summ["markets_with_programs"] == 2, summ)
    rows = sc.rows(sid, 10, 100, quoter_ok_only=False)
    a = next(r for r in rows if r["ticker"] == "KXA-1")
    b = next(r for r in rows if r["ticker"] == "KXB-1")
    exp = 10 * (10 / 210 + 10 / 210) / 2           # $10/day pool; 200 at each best level + ours 10
    check("scan: KXA-1 estimate from the formula", close(a["sizes"]["10"]["est_daily"], round(exp, 4), 1e-4), (a["sizes"]["10"], exp))
    check("scan: capital = size x (YES bid + NO bid)", close(a["sizes"]["10"]["capital"], 10 * (0.50 + 0.48)), a["sizes"]["10"])
    check("scan: pools paying under $1 for the period are zeroed", b["sizes"]["10"]["est_daily"] == 0, b["sizes"]["10"])
    check("scan: quoter filter applied", a["quoter_ok"] and b["quoter_ok"])
    al = summ["allocation"]["10"]
    check("scan: allocation fits the budget", al[0]["budget"] == 100 and al[0]["markets"] == 1 and al[0]["capital"] <= 100, al)
    check("scan stored", sc.latest(done_only=True)["id"] == sid)

    # shadow on mock: KXA-1 quoted; then a public trade through our YES bid fills it
    clock = {"t": time.time()}
    sh = mm.Shadow(wdb, client=KalshiClient(base), req_per_sec=50, clock=lambda: clock["t"])
    sh._spawn = lambda: None           # drive steps by hand
    rid = sh.start(sc, {"n_markets": 5, "size": 10, "poll_sec": 10})
    check("shadow: only markets with an estimated reward picked", set(sh.markets) == {"KXA-1"}, list(sh.markets))
    check("shadow: series fee type looked up", sh.markets["KXA-1"].get("fee_type") is not None)
    sh.step()
    qs = sh.sims[("KXA-1", "queue")].s["quotes"]
    check("shadow: first poll places virtual quotes", qs.get("yes", {}).get("px") == 0.50 and qs.get("no", {}).get("px") == 0.48, qs)
    clock["t"] += 10
    st = mock.state.S
    if st is not None:
        st["public_trades"] = [{"trade_id": "x1", "ticker": "KXA-1", "created_time": iso(clock["t"] - 2), "yes_price_dollars": "0.4500",
                                "count_fp": "7.00", "taker_book_side": "ask"}]
    sh.step()
    q, t = sh.sims[("KXA-1", "queue")].s, sh.sims[("KXA-1", "touch")].s
    check("shadow: trade through the bid fills both models", st is not None and q["pos_yes"] == 10 and t["pos_yes"] == 10, (q["pos_yes"], t["pos_yes"]))
    check("shadow: rewards accrue between polls", q["reward"] > 0 and close(q["total_sec"], 10), (q["reward"], q["total_sec"]))
    sh.step()
    check("shadow: the same trade is never counted twice", sh.sims[("KXA-1", "queue")].s["pos_yes"] == 10)
    check("shadow: fee type confirmed at start", sh.markets["KXA-1"].get("fee_known") is True and sh.markets["KXA-1"]["fee_type"] == "quadratic")
    vf = sh.verify_fees()
    check("verify fees: nothing to change when the stored type is right", vf["changed"] == [] and vf["confirmed"] == vf["series"] == 1 and vf["fees_added"] == 0, vf)
    real_series = sh.client.series
    sh.client.series = lambda t_: {"ticker": t_, "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}
    vf = sh.verify_fees(apply=False)
    check("verify fees (look only): difference reported, nothing changed", len(vf["changed"]) == 1 and sh.sims[("KXA-1", "queue")].s["fees"] == 0 and vf["applied"] is False, vf)
    vf = sh.verify_fees()
    fq = sh.sims[("KXA-1", "queue")].s["fees"]
    check("verify fees: wrong stored type corrected and every recorded fill re-priced (10 @ .50 maker = 5c)",
          close(fq, 0.05) and close(vf["fees_added"], 0.10) and sh.markets["KXA-1"]["fee_type"] == "quadratic_with_maker_fees"
          and all(close(r_["fee"], 0.05) for r_ in wdb.query("SELECT fee FROM mm_fills WHERE run_id=?", (rid,))), (fq, vf))
    sh.client.series = real_series
    vf = sh.verify_fees()
    check("verify fees: corrected back, fees return to 0", sh.sims[("KXA-1", "queue")].s["fees"] == 0 and close(vf["fees_added"], -0.10), vf)
    def boom(t_): raise RuntimeError("down")
    sh.client.series = boom
    vf = sh.verify_fees()
    check("verify fees: lookup failure -> charged as maker fees and listed as unconfirmed",
          close(sh.sims[("KXA-1", "queue")].s["fees"], 0.05) and "KXA" in vf["unconfirmed"] and sh.markets["KXA-1"]["fee_known"] is False, vf)
    sh.client.series = real_series
    sh.verify_fees()
    sh._persist()
    rep = mm.report(wdb, rid, now=clock["t"] + 7200)
    m = rep["models"]["queue"]
    check("verdict is on trading alone, rewards listed as a bonus; early runs are only 'ahead'/'behind'",
          rep["verdict"]["status"] == "ahead" and rep["verdict"]["enough_data"] is False and rep["verdict"]["trading_pnl"] == m["trading_pnl_exit"] and close(m["trading_pnl_exit"], 10 * 0.50 - 5.0, 0.011) and close(m["trading_pnl"], 0.1, 0.011)
          and rep["verdict"]["bonus_rewards"] == m["reward"] and "rewards excluded" in rep["verdict"]["basis"], rep["verdict"])
    check("report: fills, contracts and cost", m["fills"] == 1 and m["contracts"] == 10 and close(m["cost"], 5.0), m)
    check("report: inventory marked to mid, net = rewards + trading", close(m["value"], 10 * 0.51) and close(m["net"], round(m["reward"] + m["trading_pnl"], 2), 0.011), m)
    check("report: markout vs mid after fill (mid .51 vs price .50 -> +1c)", close(m["markout_cents"]["60m"], 1.0), m["markout_cents"])
    # restart: resume drops open quotes and continues
    sh2 = mm.Shadow(wdb, client=KalshiClient(base), req_per_sec=50, clock=lambda: clock["t"])
    sh2._spawn = lambda: None
    check("resume picks up the running run", sh2.resume() == [rid] and sh2.sims[("KXA-1", "queue")].s["pos_yes"] == 10)
    check("resume drops quotes that would have expired", not sh2.sims[("KXA-1", "queue")].s["quotes"])
    sh2.stop()
    check("stop marks the run stopped", wdb.query("SELECT status FROM mm_runs WHERE id=?", (rid,))[0]["status"] == "stopped")

    # several runs share one feed: same market fetched once per poll, every run fills from the same trades
    sh3 = mm.Shadow(wdb, client=KalshiClient(base), req_per_sec=50, clock=lambda: clock["t"])
    sh3._spawn = lambda: None
    st["public_trades"] = []
    ids = sh3.start_set(sc, {"n_markets": 5, "size": 10, "poll_sec": 10, "per_series": 3})
    names = [json.loads(r["params"])["name"] for r in wdb.query(f"SELECT params FROM mm_runs WHERE id IN ({','.join(map(str, ids))}) ORDER BY id")]
    check("variant set: quiet-market preset skipped when no market qualifies (mock volume 500)",
          len(ids) == 7 and not any("quiet" in n for n in names) and any("quiet" in x for x in sh3.last_skipped), (names, sh3.last_skipped))
    clock["t"] += 10
    sh3.step()
    off = next(r for r in sh3.runs.values() if r.p.get("offset_ticks"))
    base_run = next(r for r in sh3.runs.values() if "baseline" in r.p["name"])
    check("1c-behind variant quotes 1 tick below the best bid", off.sims[("KXA-1", "queue")].s["quotes"]["yes"]["px"] == 0.49
          and base_run.sims[("KXA-1", "queue")].s["quotes"]["yes"]["px"] == 0.50, off.sims[("KXA-1", "queue")].s["quotes"])
    calls_before = len(mock.state.calls)
    clock["t"] += 10
    st["public_trades"] = [{"trade_id": "y1", "ticker": "KXA-1", "created_time": iso(clock["t"] - 2), "yes_price_dollars": "0.4500",
                            "count_fp": "250.00", "taker_book_side": "ask"}]
    sh3.step()
    tr_calls = [c_ for c_ in mock.state.calls[calls_before:] if c_[1].endswith("/markets/trades")]
    check("shared feed: one trades request per market per poll for all runs", len(tr_calls) == len({c_[2].split("&")[0] for c_ in tr_calls}) and len(tr_calls) >= 1, tr_calls)
    pull = next(r for r in sh3.runs.values() if r.p.get("pull_after_fill_min"))
    ps = pull.sims[("KXA-1", "queue")].s
    check("every run fills from the same trade", all(r.sims[("KXA-1", "queue")].s["pos_yes"] == 10 for r in sh3.runs.values()))
    check("pull variant: burst (250 >= 200 contracts) pulls both sides and cools down", ps["bursts"] == 1 and not ps["quotes"]
          and ps["cooldown_until"] > clock["t"], ps)
    check("pull variant: YES side paused after its fill", ps["side_pause"].get("yes", 0) > clock["t"] and ps["fill_pauses"] >= 1, ps)
    check("baseline keeps quoting after the same trade", "yes" in base_run.sims[("KXA-1", "queue")].s["quotes"])
    reps = [mm.report(wdb, i, live=sh3, now=clock["t"]) for i in ids]
    check("reports per run carry the variant name", all(r["name"] for r in reps) and len(reps) == 7)
    sh3.stop(ids[0])
    check("stop one run keeps the others", ids[0] not in sh3.runs and len(sh3.runs) == 6
          and wdb.query("SELECT status FROM mm_runs WHERE id=?", (ids[0],))[0]["status"] == "stopped")
    sh3.stop()
    check("stop all", not sh3.runs)

    # API
    from fastapi.testclient import TestClient

    from app.config import Config
    from app.db import DB
    from app.server import create_app
    cfg = Config(Path(tmp) / "config.json")
    cfg.data["warehouse"]["base_url_override"] = base
    cfg.save()
    app = create_app(cfg, DB(Path(tmp) / "desk.db"), start_engine=False)
    c = TestClient(app)
    tok = c.get("/api/session").json()["token"]
    H = {"X-Desk-Token": tok}
    check("mm status endpoint", c.get("/api/mm").status_code == 200)
    check("scan needs the CSRF token", c.post("/api/mm/scan").status_code in (401, 403))
    r = c.post("/api/mm/scan", headers=H)
    check("scan starts", r.status_code == 200, r.text)
    for _ in range(100):
        j = c.get("/api/mm").json()
        if j["scan"] and j["scan"]["status"] != "running":
            break
        time.sleep(0.1)
    check("scan finishes via API", j["scan"]["status"] == "done", j["scan"])
    rws = c.get("/api/mm/scan/rows?size=10&all=1&limit=500").json()["rows"]
    check("scan rows: reward markets plus trading candidates from all open markets", sum(1 for r in rws if not r.get("no_program")) == 2
          and any(r.get("no_program") and r["ticker"].startswith("KXD") for r in rws) and not any(r["ticker"] == "KXC-1" for r in rws), [r["ticker"] for r in rws])
    r = c.post("/api/mm/shadow/start", headers=H, json={"n_markets": 99, "size": 10, "poll_sec": 1})
    check("shadow start clamps params", r.status_code == 200 and r.json()["params"]["n_markets"] == 40 and r.json()["params"]["poll_sec"] == 5, r.text)
    r2 = c.post("/api/mm/shadow/start", headers=H, json={"name": "behind", "offset_ticks": 9})
    check("a second run can start alongside (offset clamped to 5)", r2.status_code == 200 and r2.json()["params"]["offset_ticks"] == 5
          and len(app.state.mm[1].runs) == 2, r2.text)
    r3 = c.post("/api/mm/shadow/start_set", headers=H, json={"n_markets": 5})
    check("variant set via API (quiet preset skipped on the mock)", r3.status_code == 200 and len(r3.json()["ids"]) == 7 and r3.json()["skipped"], r3.text)
    check("shadow re-places like the live quoter (quote lifetime - 60s)", r3.json()["params"]["requeue_sec"] == 540)
    time.sleep(0.5)
    j = c.get("/api/mm").json()
    vr = c.post("/api/mm/shadow/verify_fees", headers=H)
    check("verify fees via API", vr.status_code == 200 and vr.json()["confirmed"] == vr.json()["series"] >= 1 and vr.json()["changed"] == [], vr.text)
    r9 = c.post("/api/mm/shadow/start", headers=H, json={"n_markets": 5, "select_by": "trading", "max_move": 0.03, "offset_ticks": 1, "name": "calm"})
    check("calm-market run can be started via API (max_move stored) or is refused cleanly",
          (r9.status_code == 200 and r9.json()["params"]["max_move"] == 0.03) or r9.status_code == 409, r9.text)
    if r9.status_code == 200:
        c.post(f"/api/mm/shadow/stop?run={r9.json()['id']}", headers=H)
    check("status lists every run", len(j["shadow"]["runs"]) == 9 and len(j["reports"]) == 9, j["shadow"])
    time.sleep(0.5)
    check("shadow stop", c.post("/api/mm/shadow/stop", headers=H).status_code == 200 and not app.state.mm[1].status()["alive"])
    check("report endpoint", c.get("/api/mm/report").status_code == 200)
    time.sleep(0.3)
    j2 = c.get("/api/mm", headers=H).json()
    check("stopped runs stay in the summary, marked stopped (results are not lost)", len(j2["reports"]) >= 9
          and all(r_["status"] != "running" for r_ in j2["reports"]) and j2["reports_pending"] is False, [(r_["run_id"], r_["status"]) for r_ in j2["reports"]])
    # a slow summary build must never hang the request (or the Home page): the last finished one is served meanwhile
    real_report = mm.report
    slow = {"n": 0}

    def slow_report(*a, **k):
        slow["n"] += 1
        time.sleep(3.2)
        return real_report(*a, **k)
    rid_new = c.post("/api/mm/shadow/start", headers=H, json={"n_markets": 5, "name": "slowtest"}).json()["id"]
    mm.report = slow_report
    t0 = time.time()
    j3 = c.get("/api/mm", headers=H).json()
    took = time.time() - t0
    check("slow summary build: answered in about 2s with the previous summaries and a refreshing flag", took < 3.5 and j3["reports_refreshing"] is True
          and len(j3["reports"]) == len(j2["reports"]), (round(took, 1), j3["reports_refreshing"], len(j3["reports"])))
    for _ in range(40):
        time.sleep(0.25)
        if app.state.mm_cache["busy"] is None:
            break
    mm.report = real_report
    j4 = c.get("/api/mm", headers=H).json()
    check("...and the finished build appears on the next request (new run included, old ones not rebuilt)",
          any(r_["run_id"] == rid_new for r_ in j4["reports"]) and slow["n"] == 1, (slow["n"], [r_["run_id"] for r_ in j4["reports"]]))
    c.post("/api/mm/shadow/stop", headers=H)
    check("no orders placed anywhere", not app.state.engine.db.query("SELECT 1 FROM orders_log LIMIT 1"))
    check("quoter still off", cfg.quoter["enabled"] is False)


def backtest_prune_test():
    from app.backtest import MarketSim, Strategy

    class Spam(Strategy):
        uses_maker = True

        def on_bar(self, ctx):
            ctx.cancel_all()
            ctx.buy("yes", 0.40, 1, maker=True, expire_hours=1)

    bars = [{"end_ts": 1000 + i * 60, "bid_c": 0.40, "ask_c": 0.42} for i in range(2000)]
    trades = [{"ts": 1000 + i * 60 + 30, "yes_price": 0.39, "count": 1, "taker_book_side": "ask"} for i in range(2000)]
    sim = MarketSim({"ticker": "X", "close_ts": 1000 + 2000 * 60, "result": "yes"}, bars, trades, Spam(), {}, "quadratic", 1, "through", 0)
    t0 = time.time()
    r = sim.run()
    check("backtester: live order list pruned (stays small)", len(sim.live) <= 2, len(sim.live))
    check("backtester: every order fills from the next trade through it (2000 bars -> 2000)", r["contracts"] == 2000 and time.time() - t0 < 5, r["contracts"])


def trading_first_tests():
    def row(t, yb, ya, prev=None, est=0.0, vol=0, ok=True, series=None):
        return {"ticker": t, "series": series or t, "quoter_ok": ok, "yes_bid": yb, "yes_ask": ya, "spread": round(ya - yb, 4),
                "prev": prev, "volume_24h": vol, "sizes": {"10": {"est_daily": est, "capital": 10.0}}}
    a = mm.trading_score(row("A", 0.40, 0.44))
    check("trading score: half the spread when nothing else applies", close(a["score"], 0.02) and a["fee"] == 0, a)
    b = mm.trading_score(row("B", 0.40, 0.44), "quadratic_with_maker_fees")
    check("trading score: maker fee comes off (0.0175 x .42 x .58 = 0.43c)", close(b["score"], 0.02 - 0.0043, 2e-4), b)
    c_ = mm.trading_score(row("C", 0.40, 0.44, prev=0.50))
    check("trading score: yesterday's move counts against (0.25 x 8c = 2c)", close(c_["score"], 0.0), c_)
    rows = [row("TIGHT", 0.50, 0.51, est=9.0),                 # 1c spread: big reward but fails the spread test
            row("WIDE", 0.40, 0.46, est=0.0),                  # 3c half-spread, no reward at all
            row("MID", 0.40, 0.44, est=0.0),
            row("MIDR", 0.30, 0.34, est=2.0),                  # same edge as MID, with a reward -> ranks ahead of MID
            row("MOVED", 0.40, 0.44, prev=0.60, est=5.0),      # negative edge after the move penalty
            row("FEE", 0.49, 0.51, est=1.0, series="SPORT"),   # 1c half-spread minus 0.44c fee
            row("NOPE", 0.40, 0.48, ok=False)]
    fee_of = lambda s_: {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1.0} if s_ == "SPORT" else {"fee_type": "quadratic"}
    pk = [r["ticker"] for r in mm.pick_trading_first(rows, 10, 10, 1000, 0.02, fee_of=fee_of)]
    check("trading-first: ranked by edge before rewards; a reward only breaks ties; no reward needed",
          pk == ["WIDE", "MIDR", "MID", "FEE"], pk)
    check("trading-first: big reward cannot rescue a tight or run-over market", "TIGHT" not in pk and "MOVED" not in pk)
    pk = [r["ticker"] for r in mm.pick_trading_first(rows, 10, 2, 1000, 0.02)]
    check("trading-first: respects max markets", pk == ["WIDE", "MIDR"], pk)
    pk = [r["ticker"] for r in mm.pick_trading_first(rows, 10, 10, 15, 0.02)]
    check("trading-first: respects the capital cap", pk == ["WIDE"], pk)
    calm = [row("CALM", 0.40, 0.46, prev=0.44), row("JUMPY", 0.40, 0.50, prev=0.50), row("NOPREV", 0.40, 0.46)]
    pk = [r["ticker"] for r in mm.pick_trading_first(calm, 10, 10, 1000, 0.02)]
    check("calm filter off: a market that moved 5c still qualifies on its spread", "JUMPY" in pk, pk)
    pk = [r["ticker"] for r in mm.pick_trading_first(calm, 10, 10, 1000, 0.02, max_move=0.03)]
    check("calm filter: moved more than 3c since yesterday -> excluded; 1c move and no history kept", sorted(pk) == ["CALM", "NOPREV"], pk)
    check("combined preset present (trading-first, 1c behind, exit step-in, calm)",
          any(p_.get("max_move") == 0.03 and p_.get("offset_ticks") == 1 and p_.get("exit_step_in") and p_.get("select_by") == "trading" for p_ in mm.PRESETS))

    class C:                                    # fee lookups never make a paper result look better
        def __init__(self, reply): self.reply, self.n = reply, 0
        def series(self, t):
            self.n += 1
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply
    f = mm.series_fees(C({"fee_type": "quadratic", "fee_multiplier": 1}), "S")
    check("fee lookup: confirmed type stored", f["fee_type"] == "quadratic" and f["fee_known"] is True, f)
    f = mm.series_fees(C(RuntimeError("boom")), "S")
    check("fee lookup failed -> maker fees assumed, flagged unconfirmed", f["fee_type"] == "quadratic_with_maker_fees" and f["fee_known"] is False, f)
    f = mm.series_fees(C({"ticker": "S"}), "S")
    check("fee lookup without a fee type -> maker fees assumed, flagged", f["fee_type"] == "quadratic_with_maker_fees" and f["fee_known"] is False, f)
    f = mm.series_fees(C({"fee_type": "brand_new_type"}), "S")
    check("unknown fee type -> maker fees assumed, flagged", f["fee_known"] is False and "not in the fee table" in f["fee_note"], f)


def retry_tests():
    from app.kalshi_client import KalshiError
    n, waits = {"c": 0}, []

    def flaky():
        n["c"] += 1
        if n["c"] < 3:
            raise KalshiError(429, {"message": "too many requests"})
        return "ok"
    check("429: waits and retries, then succeeds", mm.with_retry(flaky, sleep=waits.append) == "ok" and waits == [5, 15], waits)

    def bad():
        raise KalshiError(400, {"message": "bad"})
    try:
        mm.with_retry(bad, sleep=waits.append)
        ok = False
    except KalshiError as e:
        ok = e.status == 400
    check("other errors are not retried", ok and waits == [5, 15])

    def always():
        raise KalshiError(429, {"message": "x"})
    w2 = []
    try:
        mm.with_retry(always, sleep=w2.append)
        ok = False
    except KalshiError:
        ok = True
    check("gives up after 3 waits", ok and w2 == [5, 15, 45], w2)


def quoter_select_tests():
    import types

    from app.quoter import Quoter
    rows = []
    for i, (est, cap, vol, ok) in enumerate([(1.0, 10, 0, True), (3.0, 60, 5, True), (0.5, 2, 9999, True),
                                             (9.0, 10, 0, False), (0.0, 1, 0, True), (2.0, 50, 0, True)]):
        rows.append({"ticker": f"M{i}", "quoter_ok": ok, "volume_24h": vol, "sizes": {"10": {"est_daily": est, "capital": cap}}})
    pk = [r["ticker"] for r in mm.pick_for_quoter(rows, 10, 10, 100)]
    check("pick: best $/day per $ capital first, skips filtered and non-paying", pk == ["M2", "M0", "M1"], pk)
    pk = [r["ticker"] for r in mm.pick_for_quoter(rows, 10, 10, 100, max_volume_24h=100)]
    check("pick: optional 24h-volume ceiling", pk == ["M0", "M1"], pk)
    pk = [r["ticker"] for r in mm.pick_for_quoter(rows, 10, 2, 1000)]
    check("pick: stops at max_markets", pk == ["M2", "M0"], pk)
    pk = [r["ticker"] for r in mm.pick_for_quoter(rows, 10, 10, 15)]
    check("pick: never exceeds the capital cap (skips what doesn't fit)", pk == ["M2", "M0"], pk)

    port = free_port()
    serve(create_mock({}), port)
    base = f"http://127.0.0.1:{port}/trade-api/v2"
    cfg = {"size": 10, "max_markets": 5, "max_capital": 100, "min_daily_reward": 5, "pull_hours": 6, "max_spread": 0.06,
           "min_mid": 0.15, "max_mid": 0.85, "max_volume_24h": 0, "select_by": "rewards"}
    q = Quoter(types.SimpleNamespace(cfg=types.SimpleNamespace(quoter=cfg)))
    t0 = time.time()
    p = mm.Pacer(10)
    for _ in range(4):
        p()
    check("pacer spaces requests (4 calls at 10/s >= 0.3s)", time.time() - t0 >= 0.29, time.time() - t0)
    sel, msg = q.select(KalshiClient(base))
    check("quoter select: $10/day pool picked, $0.30/day pool below the minimum ignored", [x["ticker"] for x in sel] == ["KXA-1"], msg)
    check("quoter select: estimate from Kalshi's formula", close(float(sel[0]["est_daily"]), round(10 * (10 / 210 + 10 / 210) / 2, 4), 1e-4), sel)
    check("quoter select: share and capital reported", close(float(sel[0]["share"]), 10 / 210, 1e-4) and close(float(sel[0]["capital"]), 9.8), sel)
    cfg["select_by"], cfg["universe"] = "trading", "rewards"
    sel_t, msg_t = q.select(KalshiClient(base))
    check("quoter trading-first: both 2c-spread markets qualify, reward or not; the rewarded one ranks first on the tie",
          [x["ticker"] for x in sel_t] == ["KXA-1", "KXB-1"] and float(sel_t[0]["edge_cents"]) == 0.75 and "TRADING-FIRST" in msg_t, msg_t)
    cfg["min_spread"] = 0.03
    check("quoter trading-first: min spread 3c -> nothing qualifies", q.select(KalshiClient(base))[0] == [])
    cfg.pop("min_spread")
    cfg["universe"] = "all"
    sel_a, msg_a = q.select(KalshiClient(base))
    tk = [x["ticker"] for x in sel_a]
    check("quoter trading-first, all markets: candidates without any reward program are picked too, wide KXC is not",
          len(tk) == 5 and tk[:2] == ["KXA-1", "KXB-1"] and "KXA-2" in tk and "KXC-1" not in tk and "open markets screened" in msg_a, msg_a)
    cfg["min_volume_24h"] = 10 ** 6
    tk = [x["ticker"] for x in q.select(KalshiClient(base))[0]]
    check("...markets without a program need the minimum 24h volume", tk == ["KXA-1", "KXB-1"], tk)
    cfg.pop("min_volume_24h")
    cfg["select_by"] = "rewards"
    fm = q._fmt(sel)
    check("quoter select: UI format is plain strings", isinstance(fm[0]["capital"], str) and fm[0]["share"] == "4.8", fm)
    # background re-pick: the current list stays until the new one is ready; no overlapping re-picks
    calls = []

    def slow_select(client):
        calls.append(1)
        time.sleep(0.5)
        return [{"ticker": "NEW", "daily_reward": sel[0]["daily_reward"], "est_daily": sel[0]["est_daily"],
                 "share": sel[0]["share"], "capital": sel[0]["capital"]}], "new"
    q.e.client = None
    q.selected = list(sel)
    q.select = slow_select
    q._refresh_bg()
    q._refresh_bg()
    check("re-pick runs in the background (current markets kept meanwhile)", [x["ticker"] for x in q.selected] == ["KXA-1"])
    time.sleep(1.0)
    check("re-pick result applied when ready; overlapping re-pick skipped", [x["ticker"] for x in q.selected] == ["NEW"] and len(calls) == 1, (q.selected, calls))
    del q.select
    cfg["max_capital"] = 5
    sel, _ = q.select(KalshiClient(base))
    check("quoter select: capital cap below one market -> quotes nothing", sel == [])


if __name__ == "__main__":
    scoring_tests()
    sim_tests()
    backtest_prune_test()
    quoter_select_tests()
    retry_tests()
    trading_first_tests()
    integration_tests(tempfile.mkdtemp())
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)
