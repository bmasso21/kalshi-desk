"""Strategy scoreboard, the qualifying bar, and the owner's automatic-live limits (which never trade by themselves)."""
import json
import math
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fastapi.testclient import TestClient  # noqa: E402

from app import scoreboard as SB  # noqa: E402

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def close(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


def main():
    # ---- scoring ----
    rows = [{"pnl": 1.0, "cost": 0.5, "cluster": 1, "ts": 10}, {"pnl": 1.0, "cost": 0.5, "cluster": 1, "ts": 11},
            {"pnl": 0.0, "cost": 0.5, "cluster": 2, "ts": 20}, {"pnl": -1.0, "cost": 0.5, "cluster": 3, "ts": 30},
            {"pnl": -1.0, "cost": 0.5, "cluster": 3, "ts": 31}, {"pnl": -1.0, "cost": 0.5, "cluster": 3, "ts": 32}]
    s = SB.score(rows)
    mean = -1 / 6
    se = math.sqrt(((2 - 2 * mean) ** 2 + (0 - mean) ** 2 + (-3 - 3 * mean) ** 2) * 3 / 2) / 6
    check("score: totals, return on stake, and a t-stat that treats trades settling together as one", s["trades"] == 6 and s["events"] == 3
          and close(s["pnl"], -1.0) and close(s["return_pct"], round(100 * -1 / 3, 2)) and close(s["t"], round(mean / se, 2)), s)
    check("score: first and second half by time", s["first_half"]["trades"] == 3 and s["second_half"]["trades"] == 3 and s["first_half"]["pnl"] == 2.0, s)
    check("no trades -> zeros", SB.score([])["trades"] == 0)
    good = [{"pnl": 0.05 + (0.01 if i % 2 else -0.01), "cost": 0.9, "cluster": i, "ts": i} for i in range(120)]
    v = SB.judge(SB.score(good), {})
    check("a steady winner over 120 trades in 120 hours qualifies (all five rules)", v["status"] == "qualified" and all(r["ok"] for r in v["rules"]), v)
    v2 = SB.judge(SB.score(good[:50]), {})
    check("too few trades -> collecting, with progress", v2["status"] == "collecting" and v2["progress_pct"] == 50, v2)
    bad_second = good[:60] + [{**r, "pnl": -0.2} for r in good[60:]]
    check("losing in the second half -> failing, never qualified", SB.judge(SB.score(bad_second), {})["status"] == "failing")
    lumpy = [{"pnl": 0.05, "cost": 0.9, "cluster": i // 40, "ts": i} for i in range(120)]
    check("120 trades but only 3 separate hours -> not enough events", SB.judge(SB.score(lumpy), {})["status"] == "collecting")

    # ---- the app ----
    from app.config import Config
    from app.db import DB
    from app.server import create_app
    tmp = tempfile.mkdtemp()
    cfgp = Path(tmp) / "config.json"
    cfgp.write_text(json.dumps({"env": "demo", "base_url_override": "http://127.0.0.1:9/trade-api/v2", "ws_enabled": False}))
    cfg = Config(cfgp)
    app = create_app(cfg, DB(Path(tmp) / "t.db"), start_engine=False)
    wdb = app.state.wdb
    c = TestClient(app, raise_server_exceptions=False)
    HD = {"X-Desk-Token": c.get("/api/session").json()["token"]}
    for i in range(3):          # a few settled cushion paper buys, one still open
        wdb.execute("INSERT INTO gw_picks(ts, series, ticker, close_ts, check_min, strike, level, gap, side, ask, bid, last, fee, status, pnl) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (i, "KXGOLDH", f"G{i}", 3600.0 * (i + 1), 30, 1, 1, 20, "yes", .95, .94, None, .003,
                                                            "settled" if i < 2 else "open", .047 if i < 2 else None))
    r = c.get("/api/scoreboard", headers=HD)
    j = r.json()
    st = {x["key"]: x for x in j["strategies"]}
    check("scoreboard lists every paper strategy with its verdict", r.status_code == 200 and {"sports_edge", "sports_lean", "gold_cushion15", "gold_fv"} <= set(st)
          and st["gold_cushion15"]["score"]["trades"] == 2 and st["gold_cushion15"]["open"] == 1 and st["gold_cushion15"]["verdict"]["status"] == "retired", r.text[:300])   # retired 19 Nov 2026; its trades stay on the board
    check("automatic live trading starts OFF with $0 limits, and the page says the executor is not built",
          j["autolive"]["enabled"] is False and j["autolive"]["total_cap"] == 0 and j["autolive"]["executor_built"] is False)
    r = c.post("/api/autolive", headers=HD, json={"enabled": True})
    check("cannot turn on without a total cap", r.status_code == 400 and "total cap" in r.text)
    r = c.post("/api/autolive", headers=HD, json={"enabled": True, "total_cap": 50, "per_strategy_cap": 25, "per_trade_max": 5, "daily_loss_stop": 10})
    check("turning on or raising a limit asks for confirmation first (nothing saved yet)", r.status_code == 409 and r.json()["needs_confirm"]
          and "REAL MONEY" in r.json()["message"] and not (cfg.data.get("autolive") or {}).get("enabled"))
    tok = r.json()["confirm_token"]
    r = c.post("/api/autolive", headers=HD, json={"enabled": True, "total_cap": 50, "per_strategy_cap": 25, "per_trade_max": 5, "daily_loss_stop": 10, "confirm_token": tok})
    check("confirmed -> saved and audited", r.status_code == 200 and cfg.data["autolive"]["enabled"] is True and cfg.data["autolive"]["total_cap"] == 50)
    r = c.post("/api/autolive", headers=HD, json={"total_cap": 40, "daily_loss_stop": 5})
    check("lowering a limit needs no confirmation", r.status_code == 200 and cfg.data["autolive"]["total_cap"] == 40 and cfg.data["autolive"]["daily_loss_stop"] == 5)
    r = c.post("/api/autolive", headers=HD, json={"total_cap": 20})
    check("a total cap below the per-strategy cap is refused", r.status_code == 400 and cfg.data["autolive"]["total_cap"] == 40)
    r = c.post("/api/autolive", headers=HD, json={"per_strategy_cap": 60})
    check("a per-strategy cap above the total cap is refused", r.status_code == 400)
    r = c.post("/api/autolive", headers=HD, json={"total_cap": -5})
    check("negative amounts are refused", r.status_code == 400)
    check("the environment is untouched by any of this", cfg.env == "demo")
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
