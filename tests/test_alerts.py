"""Phase 1.3 tests: phone alerts (Pushover) + watchdog, against a strict Pushover mock."""
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from mock_kalshi import create_mock  # noqa: E402
from mock_pushover import create_pushover  # noqa: E402

OK, FAIL = [], []
TOKEN = "a" * 10 + "B" * 10 + "c1" * 5
USER = "u" * 10 + "S" * 10 + "e9" * 5


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def serve(app, port):
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    return srv


def main():
    from app.config import Config
    from app.db import DB
    from app.server import create_app
    from app.watchdog import Watchdog

    tmp = tempfile.mkdtemp()
    key = ed25519.Ed25519PrivateKey.generate()
    kp = Path(tmp) / "k.key"
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))
    rest = create_mock({"KEY-A": key.public_key()})
    serve(rest, 8961)
    po = create_pushover(TOKEN, USER)
    serve(po, 8962)
    PO = "http://127.0.0.1:8962/1/messages.json"
    msgs = po.state.messages

    cfgp = Path(tmp) / "config.json"
    cfgp.write_text(json.dumps({"env": "demo", "base_url_override": "http://127.0.0.1:8961/trade-api/v2",
                                "demo_key_id": "KEY-A", "demo_private_key_path": str(kp), "ws_enabled": False}))
    cfg = Config(cfgp)
    cfg.update({"alerts": {"api_url_override": PO, "fill_batch_sec": 0}})
    db = DB(Path(tmp) / "t.db")
    app = create_app(cfg, db, start_engine=False)
    eng = app.state.engine
    al = eng.alerts
    c = TestClient(app, raise_server_exceptions=False)
    H = {"X-Desk-Token": c.get("/api/session").json()["token"]}

    def flush():
        al.tick()
        al.drain()

    # ---- not configured -----------------------------------------------------------
    r = c.post("/api/alerts/test", headers=H, json={"priority": 0}).json()
    check("unconfigured -> skipped, nothing sent", r["status"] == "skipped" and not msgs, r)

    # ---- credentials via the Settings API ---------------------------------------------
    r = c.post("/api/config", headers=H, json={"alerts": {"pushover_user": "short", "pushover_token": TOKEN}})
    check("bad user key rejected with a clear error", r.status_code == 400 and "30 letters" in r.json()["error"], r.text)
    check("valid token in the same request still saved", cfg.data["alerts"]["pushover_token"] == TOKEN)
    r = c.post("/api/config", headers=H, json={"alerts": {"pushover_user": USER}})
    pub = r.json()["config"]["alerts"]
    check("creds masked in config view", pub["pushover_user"] == USER[:4] + "…" and pub["pushover_token"] == TOKEN[:4] + "…", pub)
    r = c.post("/api/config", headers=H, json={"alerts": {"pushover_user": pub["pushover_user"], "pushover_token": ""}})
    check("masked/blank values don't overwrite stored creds", cfg.data["alerts"]["pushover_user"] == USER
          and cfg.data["alerts"]["pushover_token"] == TOKEN)
    aud = " ".join(a["message"] for a in db.query("SELECT message FROM audit WHERE kind='config'"))
    check("creds never written to the audit log", USER not in aud and TOKEN not in aud)
    r = c.post("/api/config", headers=H, json={"alerts": {"api_url_override": "http://evil.example"}})
    check("browser can't redirect alerts (api_url_override ignored)", cfg.data["alerts"]["api_url_override"] == PO)
    r = c.post("/api/config", headers=H, json={"alerts": {"summary_time": "25:99"}})
    check("bad summary time rejected", r.status_code == 400 and cfg.data["alerts"]["summary_time"] == "23:50", r.text)
    r = c.post("/api/config", headers=H, json={"alerts": {"healthcheck_url": "http://plain"}})
    check("heartbeat URL must be https", r.status_code == 400, r.text)
    check("risk/auto/quoter untouched by alert saves", cfg.auto["enabled"] is False and cfg.quoter["enabled"] is False
          and cfg.risk["kill_switch"] is False)

    # ---- test alerts at every priority ---------------------------------------------------
    for p in (0, 1, -1):
        r = c.post("/api/alerts/test", headers=H, json={"priority": p}).json()
        check(f"test alert priority {p} sent", r["ok"] and msgs[-1]["priority"] == str(p), r)
    r = c.post("/api/alerts/test", headers=H, json={"priority": 2}).json()
    m = msgs[-1]
    check("emergency test has retry/expire + receipt", r["ok"] and r["receipt"] and m["retry"] == "60"
          and m["expire"] == "3600", (r, m))
    check("bad priority rejected", c.post("/api/alerts/test", headers=H, json={"priority": 5}).status_code == 400)

    # ---- Pushover rejects the token -> failed, no retry storm ------------------------------
    n0 = len(msgs)
    cfg.data["alerts"]["pushover_token"] = "Z" * 30
    t0 = time.time()
    r = al.send("test", "x", "y", 0, sync=True)
    check("invalid token -> failed with Pushover's error, no retries", r["status"] == "failed"
          and "token is invalid" in r["error"] and time.time() - t0 < 5 and len(msgs) == n0, r)
    cfg.data["alerts"]["pushover_token"] = TOKEN
    rows = c.get("/api/alerts", headers=H).json()
    check("/api/alerts lists sent + failed", {"sent", "failed"} <= {a["status"] for a in rows["alerts"]} and rows["configured"])

    # ---- startup / crash detection --------------------------------------------------------
    db.execute("DELETE FROM alerts")
    al.clean_flag.unlink(missing_ok=True)
    al.startup("9.9"); flush()
    check("first start -> normal 'started'", msgs[-1]["title"] == "Kalshi Desk started" and msgs[-1]["priority"] == "0")
    al.startup("9.9"); flush()
    check("start without clean-shutdown flag -> HIGH 'unclean stop'", "unclean" in msgs[-1]["title"]
          and msgs[-1]["priority"] == "1", msgs[-1])
    al.mark_clean("restart requested (update)")
    al.startup("9.9"); flush()
    check("after an update restart -> silent (low), says why", msgs[-1]["priority"] == "-1" and "restart requested" in msgs[-1]["message"])
    al.mark_clean("clean shutdown")
    al.startup("9.9"); flush()
    check("after other clean stop -> normal", msgs[-1]["priority"] == "0")

    # ---- fills ----------------------------------------------------------------------------
    n0 = len(msgs)
    f = {"trade_id": "T-1", "order_id": "O-1", "market_ticker": "KXB-1", "is_taker": True, "action": "buy",
         "purchased_side": "yes", "yes_price_dollars": "0.5700", "count_fp": "3.00", "fee_cost": "0.0300",
         "ts_ms": int(time.time() * 1000)}
    eng._on_feed("fill", f); flush()
    check("fill -> one normal alert with details", len(msgs) == n0 + 1 and msgs[-1]["message"] ==
          "BUY YES 3 x KXB-1 @ $0.57 (fee $0.03, taker)" and msgs[-1]["title"] == "Fill (DEMO)", msgs[-1])
    eng._on_feed("fill", f); flush()
    check("same fill again (reconnect replay) -> no duplicate alert", len(msgs) == n0 + 1)
    fn = dict(f, trade_id="T-2", purchased_side="no", is_taker=False, fee_cost="0")
    eng._on_feed("fill", fn); flush()
    check("NO fill shows the NO price (1 - yes)", "BUY NO 3 x KXB-1 @ $0.43 (fee $0.00, maker)" == msgs[-1]["message"], msgs[-1])
    cfg.data["alerts"]["fill_batch_sec"] = 1
    n0 = len(msgs)
    for i in range(3):
        eng._on_feed("fill", dict(f, trade_id=f"B-{i}"))
    flush()
    check("burst not sent before the batch window", len(msgs) == n0)
    time.sleep(1.1); flush()
    check("burst of 3 fills -> one batched alert", len(msgs) == n0 + 1 and msgs[-1]["title"] == "3 fills (DEMO)"
          and msgs[-1]["message"].count("\n") == 2, msgs[-1])
    rest.state.S["fills"] = [{"trade_id": "R-1", "fill_id": "F-R1", "ticker": "KXA-1", "action": "buy", "side": "yes",
                              "yes_price_dollars": "0.5200", "count_fp": "1.00", "fee_cost": "0.0200", "is_taker": True,
                              "created_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}]
    n0 = len(msgs)
    eng.sync_fills(); time.sleep(1.1); flush()
    check("fill found by REST fallback also alerts", len(msgs) == n0 + 1 and "KXA-1" in msgs[-1]["message"], msgs[-1])
    eng.sync_fills(); time.sleep(1.1); flush()
    check("REST re-sync doesn't re-alert", len(msgs) == n0 + 1)
    cfg.data["alerts"]["fill_batch_sec"] = 0

    # ---- kill switch / leg risk --------------------------------------------------------------
    c.post("/api/kill", headers=H, json={"on": True}); flush()
    check("manual kill -> HIGH alert", msgs[-1]["title"] == "KILL SWITCH ON" and msgs[-1]["priority"] == "1", msgs[-1])
    c.post("/api/kill", headers=H, json={"on": False})
    eng.trip_kill("Daily loss limit hit: down $30.00 today (limit $25.00)"); flush()
    m = msgs[-1]
    check("automatic kill -> EMERGENCY with reason", m["priority"] == "2" and "Daily loss limit" in m["message"]
          and m["retry"] and m["expire"], m)
    cfg.update({"risk": {"kill_switch": False}})
    db.audit("LEG_RISK", "Signal 7 partially executed - check positions NOW"); flush()
    check("leg risk -> EMERGENCY", msgs[-1]["priority"] == "2" and "LEG RISK" in msgs[-1]["title"])

    # ---- errors, throttled ---------------------------------------------------------------------
    n0 = len(msgs)
    db.audit("scan_error", "Traceback ... boom 1"); flush()
    db.audit("scan_error", "Traceback ... boom 2"); flush()
    db.audit("scan_error", "Traceback ... boom 3"); flush()
    check("repeated error -> 1 alert within throttle window", len(msgs) == n0 + 1 and msgs[-1]["priority"] == "1"
          and "boom 1" in msgs[-1]["message"])
    al._last_err["scan_error"] = 0
    db.audit("scan_error", "boom 4"); flush()
    check("next window's alert reports suppressed count", "(+2 more" in msgs[-1]["message"], msgs[-1]["message"])
    # transient network errors: only persistent ones page
    n0 = len(msgs)
    for i in range(2):
        db.audit("loss_check_error", "('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))")
    flush()
    check("1-2 network blips -> no alert", len(msgs) == n0)
    db.audit("loss_check_error", "Kalshi API 500: {'code': 'authentication_error'}"); flush()
    check("3rd network failure in 15 min -> HIGH alert with count", len(msgs) == n0 + 1 and
          msgs[-1]["message"].startswith("3 network failures in 15 min") and msgs[-1]["priority"] == "1", msgs[-1])
    al._net_hist["loss_check_error"] = [time.time() - 1000, time.time() - 990]
    al._last_err["loss_check_error"] = 0
    db.audit("loss_check_error", "Read timed out. (read timeout=15)"); flush()
    check("old blips outside the 15-min window don't count", len(msgs) == n0 + 1)

    # client: reads/cancels retry a dropped connection, new orders never do
    import requests as _rq
    real = eng.client.session.request
    calls = {"n": 0}

    def flaky(method, url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _rq.ConnectionError("('Connection aborted.', RemoteDisconnected('Remote end closed'))")
        return real(method, url, **kw)
    eng.client.session.request = flaky
    m_ = eng.client.market("KXA-1")
    check("GET retried after dropped connection", m_["ticker"] == "KXA-1" and calls["n"] == 2 and eng.client.net_retries == 1)
    calls["n"] = 0
    try:
        eng.client.create_order("KXA-1", "bid", "0.30", 1)
        raised = False
    except _rq.ConnectionError:
        raised = True
    check("order POST NOT retried after dropped connection (no double order)", raised and calls["n"] == 1, calls)
    calls["n"] = 0
    eng.client.session.request = real
    o = eng.client.create_order("KXA-1", "bid", "0.30", 1)
    eng.client.session.request = flaky
    calls["n"] = 0
    eng.client.cancel_order(o["order_id"], "KXA-1")
    check("cancel (DELETE) retried after dropped connection", calls["n"] == 2)
    eng.client.session.request = real

    eng.client.tags_by_category = lambda: 1 / 0
    r = c.get("/api/categories")
    flush()
    check("unhandled API error -> 500 + error alert", r.status_code == 500 and msgs[-1]["title"] == "Error: api_error", msgs[-1])
    db.audit("config", "x"); db.audit("connect", "x"); flush()
    check("routine audit entries don't alert", msgs[-1]["title"] == "Error: api_error")

    # ---- feed down / up ------------------------------------------------------------------------
    eng.feed.state.update(enabled=True, connected=False, error="ConnectionRefusedError")
    cfg.data["alerts"]["feed_down_min"] = 0.001
    al.on_feed("feed_down", {}); time.sleep(0.1); flush()
    check("feed down past threshold -> HIGH", msgs[-1]["title"] == "Live feed down" and msgs[-1]["priority"] == "1")
    n0 = len(msgs); flush()
    check("feed-down alert not repeated", len(msgs) == n0)
    al.on_feed("feed_up", {}); flush()
    check("feed restored -> normal", msgs[-1]["title"] == "Live feed restored")
    al.on_feed("feed_down", {}); al.on_feed("feed_up", {}); flush()
    check("short blip -> no alerts", msgs[-1]["title"] == "Live feed restored" and len(msgs) == n0 + 1)

    # ---- daily summary ---------------------------------------------------------------------------
    cfg.data["alerts"]["summary_time"] = "00:00"
    n0 = len(msgs)
    flush()
    m = msgs[-1]
    check("daily summary sent once at/after its time, low priority", len(msgs) == n0 + 1 and m["priority"] == "-1"
          and "P&L today" in m["message"] and "Fills: " in m["message"] and "Kill switch" in m["message"], m)
    flush()
    check("summary not repeated the same day", len(msgs) == n0 + 1)
    pv = c.get("/api/alerts/summary_preview").json()["text"]
    check("summary preview endpoint", "Fills:" in pv and "Orders:" in pv, pv)
    cfg.data["alerts"]["summary_time"] = "23:59"

    # ---- heartbeat ping ----------------------------------------------------------------------------
    cfg.data["alerts"]["healthcheck_url"] = "http://127.0.0.1:8962/ping"
    al._last_hc = 0
    flush()
    check("heartbeat URL pinged", po.state.pings == 1)
    flush()
    check("heartbeat at most once a minute", po.state.pings == 1)
    check("heartbeat URL masked in config view", c.get("/api/config").json()["config"]["alerts"]["healthcheck_url"].endswith("…"))
    cfg.data["alerts"]["healthcheck_url"] = ""

    # ---- disabled categories ------------------------------------------------------------------------
    cfg.data["alerts"]["fills"] = False
    n0 = len(msgs)
    eng._on_feed("fill", dict(f, trade_id="T-off")); flush()
    check("fills toggle off -> no fill alert", len(msgs) == n0)
    cfg.data["alerts"]["fills"] = True
    cfg.data["alerts"]["enabled"] = False
    r = al.send("test", "x", "y", 0, sync=True)
    check("master switch off -> skipped", r["status"] == "skipped")
    cfg.update({"alerts": {"enabled": True}})

    # ---- watchdog -------------------------------------------------------------------------------------
    cfg.save()
    clock = {"t": 1000.0}
    state = {"up": False}
    restarts = []
    wd = Watchdog(cfgp, fetch=lambda port: state["up"], restart=lambda: restarts.append(1) or "killed [1234]",
                  now=lambda: clock["t"])
    n0 = len(msgs)

    def at(t):
        clock["t"] = 1000.0 + t
        return wd.step()
    at(0); at(30); at(60); at(90)
    check("watchdog quiet before 2 min", len(msgs) == n0)
    at(120)
    m = msgs[-1] if len(msgs) > n0 else {}
    check("down 2 min -> EMERGENCY 'DOWN'", m.get("title") == "Kalshi Desk is DOWN" and m.get("priority") == "2", m)
    at(150); at(240)
    check("no repeat alert while down", len(msgs) == n0 + 1)
    at(300)
    check("still down 3 min later -> restart + HIGH alert", restarts == [1] and msgs[-1]["title"] == "Kalshi Desk restart attempted")
    at(330); at(600)
    check("no restart loop", restarts == [1])
    state["up"] = True
    at(630)
    check("recovery -> 'back up' with outage length", msgs[-1]["title"] == "Kalshi Desk is back up"
          and "10.5 min" in msgs[-1]["message"], msgs[-1])
    n0 = len(msgs)
    state["up"] = False; at(700); at(720); state["up"] = True; at(730)
    check("20-30s update restart -> no alerts", len(msgs) == n0)
    check("watchdog writes heartbeat", (Path(tmp) / "logs" / "watchdog.heartbeat").exists())
    from app.watchdog import heartbeat_fresh
    check("heartbeat freshness check", heartbeat_fresh(Path(tmp)) is False)   # simulated clock is in 1970
    (Path(tmp) / "logs" / "watchdog.heartbeat").write_text(str(time.time()))
    check("fresh heartbeat -> no second watchdog spawned", heartbeat_fresh(Path(tmp)) is True)

    # ---- full engine stop marks a clean shutdown --------------------------------------------------------
    al.clean_flag.unlink(missing_ok=True)
    eng.stop("restart requested (update)")
    check("engine.stop writes the clean-shutdown flag with reason",
          al.clean_flag.read_text() == "restart requested (update)")


if __name__ == "__main__":
    main()
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)
