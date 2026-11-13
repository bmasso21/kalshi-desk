"""Local web server. Binds to 127.0.0.1 only - nothing is exposed to your network."""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from . import __version__, risk, scaling
from .config import Config
from .db import DB
from .engine import NEVER_AUTO, Engine
from .fees import fee
from .config import BASE_URLS
from .kalshi_client import KalshiClient, KalshiError, book_from_orderbook, d, load_private_key

STATIC = Path(__file__).parent / "static"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def create_app(cfg: Config | None = None, db: DB | None = None, start_engine: bool = True) -> FastAPI:
    cfg = cfg or Config()
    db = db or DB()
    eng = Engine(cfg, db)
    from .backtest import STRATEGIES, Backtester
    from .warehouse import Warehouse, WarehouseDB
    wdb = WarehouseDB(cfg.path.parent / "warehouse.db")
    eng.warehouse = Warehouse(eng, wdb)
    eng.feed.listeners.append(eng.warehouse.on_feed)
    from .weather import WxCollector, WxModel, WxStore
    from .weather import board as wx_board
    wx_store = WxStore(wdb)
    from .sports import BudgetExceeded, OddsPapi
    from .sports import Study
    from .sports import discover as sp_discover
    from .sports import evaluate as sp_evaluate
    oddspapi = OddsPapi(lambda: cfg.data.get("sports") or {}, wdb)
    app_sp_state = {"running": False}
    eng.weather = WxCollector(eng, wx_store)
    from .maintenance import Maintenance
    maint = Maintenance(eng, wdb)
    from . import mm as mmod
    mm_scanner = mmod.Scanner(wdb, client=KalshiClient(cfg.data.get("warehouse", {}).get("base_url_override") or BASE_URLS["prod"]))
    mm_shadow = mmod.Shadow(wdb, client=KalshiClient(cfg.data.get("warehouse", {}).get("base_url_override") or BASE_URLS["prod"]))
    wx_model: dict = {"m": None, "ts": 0.0}

    def weather_model():
        if wx_model["m"] is None or time.time() - wx_model["ts"] > 600:
            wx_model["m"], wx_model["ts"] = WxModel(wx_store), time.time()
        return wx_model["m"]
    bt = Backtester(wdb)
    from .studies import KINDS as STUDY_KINDS, Studies
    studies = Studies(wdb)
    studies.recover()
    from .auth import COOKIE, Auth
    auth = Auth(cfg, db, eng.alerts, extra_local_hosts=() if start_engine else ("testclient",))
    app = FastAPI(title="Kalshi Desk", version=__version__)
    app.state.engine = eng
    app.state.backtester = bt
    app.state.auth = auth
    app.state.wdb = wdb
    app.state.mm = (mm_scanner, mm_shadow)
    app.state.maint = maint
    session_token = secrets.token_urlsafe(24)  # CSRF guard for state-changing calls
    prod_confirms: dict[str, float] = {}

    @app.on_event("startup")
    def _start():
        if start_engine:
            eng.start()
            eng.warehouse.start()
            eng.weather.start()
            maint.start()
            try:
                mm_shadow.resume()
            except Exception as e:
                db.audit("mm_error", f"resume: {e}"[:500])
            eng.alerts.startup(__version__)
            if getattr(cfg, "recovered", None):
                db.audit("connect_error", f"SETTINGS FILE WAS DAMAGED: {cfg.recovered}. Check Settings (keys, limits, alerts).")

    @app.on_event("shutdown")
    def _stop():
        maint.stop()
        mm_shadow.shutdown()
        eng.weather.stop()
        eng.warehouse.stop()
        eng.stop()

    @app.exception_handler(KalshiError)
    async def _kerr(_, e: KalshiError):
        return JSONResponse({"error": str(e), "detail": e.payload}, status_code=400)

    @app.exception_handler(Exception)
    async def _any(request: Request, e: Exception):
        db.audit("api_error", f"{request.method} {request.url.path}: {type(e).__name__}: {e}")
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)

    @app.exception_handler(risk.RiskError)
    async def _rerr(_, e: risk.RiskError):
        return JSONResponse({"error": f"Risk check: {e}"}, status_code=400)

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        ok, code, msg = auth.check(request)
        if not ok:
            if request.url.path.startswith("/api/"):
                return JSONResponse({"error": msg, "login": code == 401}, status_code=code)
            if code == 401:
                return RedirectResponse("/login", status_code=303)
            return HTMLResponse(f"<!doctype html><meta name=viewport content='width=device-width'><body style='font:16px sans-serif;"
                                f"background:#0f1214;color:#e6e9eb;padding:24px'><h2>Kalshi Desk</h2><p>{msg}</p>", status_code=code)
        if request.method in ("POST", "PUT", "DELETE") and request.url.path.startswith("/api/") \
                and request.url.path != "/api/auth/login":
            if request.headers.get("X-Desk-Token") != session_token:
                return JSONResponse({"error": "bad session token - reload the page"}, status_code=403)
        return await call_next(request)

    # ---------------------------------------------------------------- auth --
    @app.get("/login")
    def login_page():
        return FileResponse(STATIC / "login.html")

    @app.get("/api/auth/status")
    def auth_status(request: Request):
        sess = auth.session(request)
        return {"password_set": auth.password_set, "local": auth.is_local(request),
                "tailscale_login": auth.ts_login(request), "logged_in": bool(sess) or auth.is_local(request),
                "allowed_tailscale_logins": auth.conf.get("allowed_tailscale_logins") or [],
                "remote_url": cfg.data.get("remote_url") or ""}

    @app.post("/api/auth/login")
    async def auth_login(request: Request):
        b = await request.json()
        ok, msg, tok = auth.login(request, str(b.get("password") or ""))
        if not ok:
            return JSONResponse({"error": msg}, status_code=401)
        r = JSONResponse({"ok": True})
        r.set_cookie(COOKIE, tok, max_age=int(float(auth.conf.get("session_days", 30)) * 86400), httponly=True,
                     secure=not auth.is_local(request), samesite="strict", path="/")
        return r

    @app.post("/api/auth/logout")
    async def auth_logout(request: Request):
        b = await request.json() if (request.headers.get("content-length") or "0") != "0" else {}
        auth.logout(request, everywhere=bool(b.get("everywhere")))
        r = JSONResponse({"ok": True})
        r.delete_cookie(COOKIE, path="/")
        return r

    @app.post("/api/auth/password")
    async def auth_password(request: Request):
        b = await request.json()
        ok, msg = auth.set_password(request, str(b.get("new") or ""), b.get("current"))
        if not ok:
            return JSONResponse({"error": msg}, status_code=400)
        r = JSONResponse({"ok": True, "message": msg})
        r.delete_cookie(COOKIE, path="/")
        return r

    @app.get("/api/auth/sessions")
    def auth_sessions():
        return {"sessions": auth.sessions()}

    # ------------------------------------------------------------------ ui --
    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/session")
    def session():
        return {"token": session_token, "version": __version__}

    @app.get("/api/status")
    def status():
        c = eng.client
        fs = eng.feed.status()
        out = {"env": cfg.env, "authenticated": c.authenticated, "scan": eng.scan_state,
               "kill_switch": cfg.risk["kill_switch"], "auto": cfg.auto["enabled"],
               "feed": {k: fs[k] for k in ("mode", "connected", "reason", "error", "markets_watched",
                                           "books_synced", "since", "last_msg")}}
        try:
            out["exchange"] = c.exchange_status()
        except Exception as e:
            out["exchange_error"] = str(e)
        return out

    # ------------------------------------------------------------- config --
    @app.get("/api/config")
    def get_config():
        return {"config": cfg.public_view(), "never_auto": sorted(NEVER_AUTO)}

    @app.post("/api/config")
    async def set_config(request: Request):
        patch = await request.json()
        for k in ("demo_private_key_path", "prod_private_key_path", "demo_key_id", "prod_key_id"):
            if k in patch and (patch[k] in ("", None, True, False) or str(patch[k]).endswith("…")):
                patch.pop(k)  # don't overwrite secrets with masked values
        errors = []
        for e in ("demo", "prod"):
            kid = f"{e}_key_id"
            if kid in patch:
                patch[kid] = str(patch[kid]).strip()
                if not UUID_RE.fullmatch(patch[kid].lower()):
                    patch.pop(kid)
                    errors.append(f"{e} Key ID is not a Kalshi Key ID (expected format "
                                  f"xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx). Don't paste the private key there.")
            pk = f"{e}_private_key_path"
            if pk in patch:
                p = str(patch[pk]).strip().strip('"').strip("'")
                try:
                    load_private_key(p)
                    patch[pk] = p
                except FileNotFoundError:
                    patch.pop(pk)
                    errors.append(f"{e} key file not found: {p}")
                except Exception:
                    patch.pop(pk)
                    errors.append(f"{e} key file is not a valid private key: {p}")
        if "auth" in patch:
            a = patch["auth"] if isinstance(patch["auth"], dict) else {}
            a = {k: v for k, v in a.items() if k in ("allowed_tailscale_logins", "session_days")}
            if "allowed_tailscale_logins" in a:
                v = a["allowed_tailscale_logins"]
                if not isinstance(v, list) or not all(isinstance(x, str) and "@" in x or isinstance(x, str) and len(x) < 100 for x in v):
                    a.pop("allowed_tailscale_logins")
                    errors.append("allowed Tailscale logins must be a list of account names")
                else:
                    v = [x.strip() for x in v if x.strip()]
                    me = (auth.ts_login(request) or "").lower()
                    if v and not auth.is_local(request) and me not in [x.lower() for x in v]:
                        a.pop("allowed_tailscale_logins")
                        errors.append(f"That list doesn't include you ({me or 'unknown'}) - refusing to lock you out")
                    else:
                        a["allowed_tailscale_logins"] = v
            if "session_days" in a:
                try:
                    a["session_days"] = max(1.0, min(365.0, float(a["session_days"])))
                except (TypeError, ValueError):
                    a.pop("session_days")
            patch["auth"] = a
            if not a:
                patch.pop("auth")
        if "remote_url" in patch:
            u = str(patch.get("remote_url") or "").strip().rstrip("/")
            if not auth.is_local(request):
                patch.pop("remote_url")
            elif u and not re.fullmatch(r"https://[A-Za-z0-9.-]+\.ts\.net", u):
                patch.pop("remote_url")
                errors.append("remote_url must look like https://<machine>.<tailnet>.ts.net")
            else:
                patch["remote_url"] = u
        if isinstance(patch.get("breakers"), dict):
            bp = {k: v for k, v in patch["breakers"].items()
                  if k in ("enabled", "check_min", "strategy_daily_loss", "strategy_7d_loss", "account_drawdown", "drawdown_days")}
            for k, lo, hi in (("check_min", 1, 60), ("strategy_daily_loss", 0, 10 ** 6), ("strategy_7d_loss", 0, 10 ** 6),
                              ("account_drawdown", 0, 10 ** 7), ("drawdown_days", 1, 365)):
                if k in bp:
                    try:
                        bp[k] = max(lo, min(hi, float(bp[k])))
                    except (TypeError, ValueError):
                        bp.pop(k)
            if "enabled" in bp:
                bp["enabled"] = bool(bp["enabled"])
            patch["breakers"] = bp            # 'tripped' / peak reset only via the reset endpoint
        if isinstance(patch.get("maintenance"), dict):
            mp_ = {}
            src = patch["maintenance"]
            if "enabled" in src:
                mp_["enabled"] = bool(src["enabled"])
            if re.fullmatch(r"[0-2]?\d:[0-5]\d", str(src.get("time") or "")):
                mp_["time"] = str(src["time"])
            for k, lo, hi in (("keep_days", 1, 60), ("prune_days", 7, 3650), ("warehouse_max_gb", 0, 500)):
                if k in src:
                    try:
                        mp_[k] = max(lo, min(hi, float(src[k])))
                    except (TypeError, ValueError):
                        pass
            patch["maintenance"] = mp_
        if isinstance(patch.get("exits"), dict):
            ex = {}
            if "enabled" in patch["exits"]:
                ex["enabled"] = bool(patch["exits"]["enabled"])
            if "confirm_checks" in patch["exits"]:
                try:
                    ex["confirm_checks"] = max(1, min(10, int(patch["exits"]["confirm_checks"])))
                except (TypeError, ValueError):
                    pass
            if "check_sec" in patch["exits"]:
                try:
                    ex["check_sec"] = max(10, min(600, float(patch["exits"]["check_sec"])))
                except (TypeError, ValueError):
                    pass
            patch["exits"] = ex
        want_rescale = False
        if isinstance(patch.get("scaling"), dict):
            sp_ = patch["scaling"]
            sc = {}
            if "enabled" in sp_:
                sc["enabled"] = bool(sp_["enabled"])
                want_rescale = sc["enabled"] and not (cfg.data.get("scaling") or {}).get("enabled")
            if "max_markets_cap" in sp_:
                try:
                    sc["max_markets_cap"] = max(1, min(50, int(sp_["max_markets_cap"])))
                except (TypeError, ValueError):
                    pass
            if isinstance(sp_.get("caps"), dict):
                caps = {}
                for k, v in sp_["caps"].items():
                    if k in scaling.KEYS:
                        try:
                            if float(v) > 0:
                                caps[k] = float(v)
                        except (TypeError, ValueError):
                            errors.append(f"cap for {k} must be a number")
                cfg.data.setdefault("scaling", {})["caps"] = caps        # replace, not merge
                sc["caps"] = caps
                want_rescale = want_rescale or bool((cfg.data.get("scaling") or {}).get("enabled") or sc.get("enabled"))
            if isinstance(sp_.get("tiers"), list):
                try:
                    tiers = [{"upto": (None if t.get("upto") in (None, "", 0) else float(t["upto"])),
                              **{k: max(0.0, min(100.0, float(t[k]))) for k in scaling.KEYS}} for t in sp_["tiers"]]
                    ups = [t["upto"] for t in tiers]
                    if not tiers or ups[-1] is not None or any(u is None for u in ups[:-1]) or ups[:-1] != sorted(ups[:-1]):
                        raise ValueError
                    sc["tiers"] = tiers
                    want_rescale = True
                except (KeyError, TypeError, ValueError):
                    errors.append("tiers must be ordered by 'upto' (last one open-ended) with a percentage 0-100 for every limit")
            patch["scaling"] = sc           # base / limits are only ever set by the app
        scaling_on = bool({**(cfg.data.get("scaling") or {}), **(patch.get("scaling") or {})}.get("enabled"))
        if scaling_on:                      # dollar limits follow the account: ignore hand-typed values for them
            for sec, keys in scaling.SCALED_FIELDS.items():
                if isinstance(patch.get(sec), dict):
                    for k in keys:
                        patch[sec].pop(k, None)
        tripped = (cfg.data.get("breakers") or {}).get("tripped") or {}
        if isinstance(patch.get("quoter"), dict) and patch["quoter"].get("enabled") and not cfg.quoter.get("enabled") \
                and "liquidity" in tripped:
            patch["quoter"].pop("enabled")
            errors.append("The quoter's circuit breaker is tripped - reset it in Settings first")
        if isinstance(patch.get("auto"), dict) and isinstance(patch["auto"].get("strategies"), list):
            blocked = [x for x in patch["auto"]["strategies"] if x in tripped and x not in (cfg.auto.get("strategies") or [])]
            if blocked:
                patch["auto"]["strategies"] = [x for x in patch["auto"]["strategies"] if x not in blocked]
                errors.append(f"Circuit breaker tripped for {', '.join(blocked)} - reset it first")
        if isinstance(patch.get("quoter"), dict):
            qp = patch["quoter"]
            if "universe" in qp and qp["universe"] not in ("all", "rewards"):
                qp.pop("universe")
            for k, lo, hi in (("market_loss_pct", 0, 100), ("min_volume_24h", 0, 10 ** 9), ("max_move", 0, 0.5), ("pull_after_fill_min", 0, 240)):
                if k in qp:
                    try:
                        qp[k] = max(lo, min(hi, float(qp[k])))
                    except (TypeError, ValueError):
                        qp.pop(k)
            if "select_by" in qp and qp["select_by"] not in ("trading", "rewards"):
                qp.pop("select_by")
                errors.append("quoter select_by must be 'trading' or 'rewards'")
            if "min_spread" in qp:
                try:
                    qp["min_spread"] = max(0.01, min(0.2, float(qp["min_spread"])))
                except (TypeError, ValueError):
                    qp.pop("min_spread")
            if "exit_step_in" in qp:
                qp["exit_step_in"] = bool(qp["exit_step_in"])
            if "offset_ticks" in qp:
                try:
                    qp["offset_ticks"] = max(0, min(5, int(float(qp["offset_ticks"]))))
                except (TypeError, ValueError):
                    qp.pop("offset_ticks")
            for k, lo, hi in (("quote_ttl_sec", 120, 3600), ("max_volume_24h", 0, 10 ** 9)):
                if k in qp:
                    try:
                        qp[k] = max(lo, min(hi, float(qp[k])))
                    except (TypeError, ValueError):
                        qp.pop(k)
        if isinstance(patch.get("warehouse"), dict):
            w = patch["warehouse"]
            w.pop("base_url_override", None)         # test-only, never from the browser
            for k in ("categories", "series", "tags"):
                if k in w:
                    if not isinstance(w[k], list) or not all(isinstance(x, str) and 0 < len(x) < 80 for x in w[k]):
                        w.pop(k)
                        errors.append(f"warehouse {k} must be a list of names")
                    else:
                        w[k] = [x.strip() for x in w[k] if x.strip()]
            for k, lo, hi in (("lookback_days", 1, 730), ("req_per_sec", 0.5, 10), ("sync_interval_min", 10, 1440)):
                if k in w:
                    try:
                        v = float(w[k])
                        assert lo <= v <= hi
                        w[k] = v
                    except Exception:
                        w.pop(k)
                        errors.append(f"warehouse {k} must be between {lo} and {hi}")
            if "sample" in w:
                sm, okd = w["sample"], {}
                if isinstance(sm, dict):
                    if "enabled" in sm:
                        okd["enabled"] = bool(sm["enabled"])
                    for k, lo, hi in (("series_per_category", 1, 100), ("markets_per_series", 1, 1000), ("days", 1, 365), ("max_trades_per_market", 100, 200000)):
                        if k in sm:
                            try:
                                okd[k] = max(lo, min(hi, int(float(sm[k]))))
                            except (TypeError, ValueError):
                                pass
                    if isinstance(sm.get("overrides"), dict):      # per-category depth, e.g. {"Sports": {"series_per_category": 40}}
                        ovs = {}
                        for cat, ov in list(sm["overrides"].items())[:20]:
                            if not isinstance(ov, dict):
                                continue
                            o2 = {}
                            for k, lo, hi in (("series_per_category", 1, 200), ("markets_per_series", 1, 2000), ("days", 1, 365), ("max_trades_per_market", 100, 200000)):
                                if k in ov:
                                    try:
                                        o2[k] = max(lo, min(hi, int(float(ov[k]))))
                                    except (TypeError, ValueError):
                                        pass
                            ovs[str(cat)[:60]] = o2
                        cfg.data.setdefault("warehouse", {}).setdefault("sample", {})["overrides"] = ovs   # replaced whole, not merged
                    if isinstance(sm.get("skip_categories"), list):
                        cfg.data.setdefault("warehouse", {}).setdefault("sample", {})["skip_categories"] = [str(x)[:60] for x in sm["skip_categories"]][:40]
                w["sample"] = okd
            if "candle_period" in w and int(w.get("candle_period") or 0) not in (1, 60, 1440):
                w.pop("candle_period")
                errors.append("candle_period must be 1, 60 or 1440")
        if isinstance(patch.get("alerts"), dict):
            al = patch["alerts"]
            for k, label in (("pushover_user", "Pushover user key"), ("pushover_token", "Pushover API token")):
                if k in al:
                    v = str(al[k] or "").strip()
                    if not v or v.endswith("…"):
                        al.pop(k)            # blank / masked value = keep the stored one
                    elif not re.fullmatch(r"[A-Za-z0-9]{30}", v):
                        al.pop(k)
                        errors.append(f"{label} should be 30 letters/digits (copy it from pushover.net)")
                    else:
                        al[k] = v
            if "healthcheck_url" in al:
                v = str(al["healthcheck_url"] or "").strip()
                if v.endswith("…"):
                    al.pop("healthcheck_url")
                elif v and not v.startswith("https://"):
                    al.pop("healthcheck_url")
                    errors.append("Heartbeat URL must start with https://")
                else:
                    al["healthcheck_url"] = v
            if "summary_time" in al and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(al["summary_time"])):
                al.pop("summary_time")
                errors.append("Summary time must be HH:MM (24h)")
            for k in ("api_url_override",):
                al.pop(k, None)              # test-only setting, never from the browser
        if isinstance(patch.get("sports"), dict):
            sp = {k: v for k, v in patch["sports"].items() if k in ("oddspapi_key", "monthly_cap")}
            if "oddspapi_key" in sp:
                v = str(sp["oddspapi_key"] or "").strip()
                if not v or v.endswith("…"):
                    sp.pop("oddspapi_key")
                elif not re.fullmatch(r"[A-Za-z0-9_\-]{16,128}", v):
                    sp.pop("oddspapi_key")
                    errors.append("OddsPapi key should be 16-128 letters/digits (copy it from your OddsPapi dashboard)")
                else:
                    sp["oddspapi_key"] = v
            if "monthly_cap" in sp:
                try:
                    sp["monthly_cap"] = max(0, min(250, int(sp["monthly_cap"])))
                except (TypeError, ValueError):
                    sp.pop("monthly_cap")
                    errors.append("monthly cap must be a whole number 0-250")
            patch["sports"] = sp
            if not sp:
                patch.pop("sports")
        if "auto" in patch and "strategies" in patch["auto"]:
            patch["auto"]["strategies"] = [s for s in patch["auto"]["strategies"] if s not in NEVER_AUTO]
        old_env = cfg.env
        if patch.get("env") and patch["env"] != old_env:
            eng.quoter.cancel_all("environment switch")
            eng.quoter.selected, eng.quoter.programs_ts = [], 0     # re-pick markets for the new environment
        cfg.update(patch)
        post_update_rescale = want_rescale or (scaling_on and patch.get("env") and patch["env"] != old_env)
        if any(k in patch for k in ("env", "base_url_override")) or any(k.endswith(("_key_id", "_key_path")) for k in patch):
            eng.connect()
        safe = {k: v for k, v in patch.items() if "key" not in k}
        if isinstance(safe.get("sports"), dict):
            safe["sports"] = {k: v for k, v in safe["sports"].items() if k != "oddspapi_key"}
        if isinstance(safe.get("alerts"), dict):
            safe["alerts"] = {k: v for k, v in safe["alerts"].items() if k not in ("pushover_user", "pushover_token", "healthcheck_url")}
        if post_update_rescale:
            try:
                eng.scaler.apply(force=True)
            except Exception as e:
                errors.append(f"could not read the account value to scale the limits: {e}")
        db.audit("config", json.dumps(safe)[:1000])
        if cfg.env != old_env:
            eng.trigger_scan()
        out = get_config()
        out["errors"] = errors
        out["authenticated"] = eng.client.authenticated
        if errors:
            return JSONResponse({**out, "error": " | ".join(errors)}, status_code=400)
        return out

    @app.post("/api/kill")
    async def kill(request: Request):
        on = (await request.json()).get("on", True)
        if on:
            cancelled = eng.trip_kill("manual kill switch")
        else:
            cfg.update({"risk": {"kill_switch": False}})
            db.audit("kill_switch", "released")
            cancelled = []
        return {"kill_switch": bool(on), "cancelled": cancelled}

    # -------------------------------------------------------------- alerts --
    @app.get("/api/alerts")
    def alerts_list():
        return {"configured": eng.alerts.configured(),
                "alerts": db.query("SELECT id,ts,kind,priority,title,message,status,detail FROM alerts ORDER BY id DESC LIMIT 100")}

    @app.post("/api/alerts/test")
    async def alerts_test(request: Request):
        b = await request.json()
        pr = int(b.get("priority", 0))
        if pr not in (-1, 0, 1, 2):
            raise HTTPException(400, "priority must be -1, 0, 1 or 2")
        names = {-1: "low (no sound)", 0: "normal", 1: "high", 2: "EMERGENCY - repeats until you acknowledge it"}
        return eng.alerts.send("test", f"Kalshi Desk test: {names[pr]}",
                               f"Test alert at {time.strftime('%H:%M:%S')}. Env {cfg.env.upper()}.", pr, sync=True)

    @app.get("/api/alerts/summary_preview")
    def alerts_summary_preview():
        return {"text": eng.alerts.summary_text()}

    # ----------------------------------------------------------------- CLV --
    @app.get("/api/clv")
    def clv_summary(days: float = 30):
        return {**eng.clv.summary(days), "tracker": eng.clv.state}

    @app.post("/api/clv/run")
    def clv_run():
        return eng.clv.run_once()

    # ------------------------------------------------------------ research --
    @app.get("/api/research")
    def research():
        return {"warehouse": eng.warehouse.status(),
                "strategies": [{"name": k, "description": v.description, "defaults": v.defaults,
                                "uses_maker": v.uses_maker} for k, v in STRATEGIES.items()]}

    @app.post("/api/research/sync")
    def research_sync(restart: int = 0):
        """restart=1: end the pass in progress and begin a new one now (used after changing what to collect)."""
        eng.warehouse.trigger(restart=bool(restart))
        return {"ok": True, "restart": bool(restart)}

    # ------------------------------------------------------------- weather --
    @app.get("/api/weather")
    def weather_status():
        return {"collector": eng.weather.status(), "series": wx_store.series_map(), "eval": wdb.get("wx_eval")}

    @app.post("/api/weather/sync")
    def weather_sync():
        eng.weather.trigger()
        return {"ok": True}

    @app.get("/api/weather/board")
    def weather_board(days: int = 1):
        return {"events": wx_board(wdb, weather_model(), max_days_ahead=max(0, min(2, days))),
                "model_built": wx_model["ts"], "note": "Quotes are the latest hourly candle from production public data."}

    @app.post("/api/weather/eval")
    def weather_eval():
        cur = wdb.get("wx_eval") or {}
        if cur.get("status") == "running" and time.time() - (cur.get("started") or 0) < 3 * 3600:
            raise HTTPException(409, "an evaluation is already running")
        import os
        import subprocess
        import sys
        wdb.put("wx_eval", {"status": "running", "started": time.time()})
        kw = {"cwd": str(Path(__file__).resolve().parent.parent), "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            kw["creationflags"] = 0x00004000 | 0x08000000
        app.state.wx_eval_proc = subprocess.Popen([sys.executable, "-m", "app.weather", "--db", wdb.path, "--eval", "--store"], **kw)
        return {"ok": True}

    # -------------------------------------------------------------- sports --
    @app.get("/api/sports")
    def sports_status():
        st = Study(oddspapi, wdb, eng.warehouse.client, eng.warehouse)
        return {"configured": oddspapi.configured, "usage": oddspapi.usage(), "running": app_sp_state["running"],
                "calls": oddspapi.calls(60), "discover": wdb.get("sp_discover"), "study": st.summary(),
                "eval": wdb.get("sp_eval")}

    @app.post("/api/sports/study")
    async def sports_study(request: Request):
        b = await request.json()
        if not oddspapi.configured:
            raise HTTPException(400, "Add your OddsPapi key in Settings first")
        if app_sp_state["running"]:
            raise HTTPException(409, "already running")
        leagues = [x for x in (b.get("leagues") or ["NFL", "NBA"]) if x in ("NFL", "NBA", "NHL", "MLB")]
        max_games = max(1, min(200, int(b.get("max_games") or 10)))
        days_back = max(1.0, min(120.0, float(b.get("days_back") or 21)))
        need = max_games + 3 * len(leagues) + 2
        if oddspapi.usage()["left"] < need:
            raise HTTPException(400, f"needs up to {need} OddsPapi requests; {oddspapi.usage()['left']} left this month")

        refresh = bool(b.get("refresh_fixtures"))

        def run():
            app_sp_state["running"] = True
            try:
                Study(oddspapi, wdb, eng.warehouse.client, eng.warehouse).run(leagues, days_back, max_games, refresh)
            except Exception as e:
                db.audit("sports_error", str(e)[:500])
            finally:
                app_sp_state["running"] = False
        import threading
        threading.Thread(target=run, daemon=True, name="sports-study").start()
        return {"ok": True, "max_requests": need}

    @app.post("/api/sports/stop")
    def sports_stop():
        wdb.put("sp_stop", time.time())
        return {"ok": True}

    @app.post("/api/sports/eval")
    def sports_eval():
        return sp_evaluate(wdb)

    @app.post("/api/sports/discover")
    def sports_discover():
        if not oddspapi.configured:
            raise HTTPException(400, "Add your OddsPapi key in Settings first")
        if app_sp_state["running"]:
            raise HTTPException(409, "already running")
        if oddspapi.usage()["left"] < 9:
            raise HTTPException(400, "not enough OddsPapi requests left this month")

        def run():
            app_sp_state["running"] = True
            try:
                sp_discover(oddspapi, eng.warehouse.client)
            except Exception as e:
                db.audit("sports_error", str(e)[:500])
            finally:
                app_sp_state["running"] = False
        import threading
        threading.Thread(target=run, daemon=True, name="sports-discover").start()
        return {"ok": True}

    @app.get("/api/sports/raw/{k}")
    def sports_raw(k: str, chars: int = 20000):
        r = oddspapi.raw(k)
        if not r:
            raise HTTPException(404, "not found")
        txt = json.dumps(r["body"])
        return {"ts": r["ts"], "chars": len(txt), "body_text": txt[:max(100, min(chars, 200000))]}

    @app.get("/api/studies")
    def studies_list(category: str = "", series: str = ""):
        flt = {"categories": [category]} if category else None
        if series:
            flt = {"series": [x.strip()[:80] for x in series.split(",") if x.strip()][:50]}
        return {"studies": studies.latest(flt), "kinds": list(STUDY_KINDS), "category": category,
                "scope": wdb.query("SELECT category, COUNT(*) markets, SUM(result IN ('yes','no')) settled FROM wh_markets GROUP BY category")}

    @app.post("/api/studies/run")
    async def studies_run(request: Request):
        b = await request.json()
        from .studies import RUN as STUDY_RUN
        kinds = [k for k in (b.get("kinds") or STUDY_KINDS) if k in STUDY_RUN]      # "scalp" runs only when named
        flt = {k: [str(x)[:80] for x in b[k]][:50] for k in ("categories", "series") if isinstance(b.get(k), list) and b[k]}
        started, skipped = [], []
        for k in kinds:
            try:
                started.append(studies.start(k, flt))
            except RuntimeError as e:
                skipped.append(str(e))
        return {"started": started, "skipped": skipped}

    @app.get("/api/backtests")
    def backtests():
        rows = wdb.query("SELECT id,ts,strategy,params,filters,status,error,duration,results FROM wh_backtests "
                         "ORDER BY id DESC LIMIT 100")
        for r in rows:
            res = json.loads(r.pop("results") or "{}")
            r["params"] = json.loads(r["params"] or "{}")
            r["filters"] = json.loads(r["filters"] or "{}")
            r["summary"] = {m: {k: v.get(k) for k in ("markets_traded", "fills", "contracts", "pnl", "roi", "fees",
                                                      "avg_clv_cents", "max_drawdown", "win_rate_markets")}
                            for m, v in (res.get("models") or {}).items()}
            r["markets_in_scope"] = res.get("markets_in_scope")
        return {"backtests": rows}

    @app.post("/api/backtests")
    async def backtest_run(request: Request):
        b = await request.json()
        if b.get("strategy") not in STRATEGIES:
            raise HTTPException(400, "unknown strategy")
        models = [m for m in (b.get("models") or []) if m in ("through", "touch")] or None
        rid = bt.start(b["strategy"], b.get("params") or {}, b.get("filters") or {}, models,
                       float(b.get("slippage_cents") or 0))
        return {"id": rid}

    @app.get("/api/backtests/{rid}")
    def backtest_get(rid: int):
        rows = wdb.query("SELECT * FROM wh_backtests WHERE id=?", (rid,))
        if not rows:
            raise HTTPException(404, "not found")
        r = rows[0]
        r["results"] = json.loads(r["results"] or "null")
        r["params"] = json.loads(r["params"] or "{}")
        r["filters"] = json.loads(r["filters"] or "{}")
        return r

    # ---------------------------------------------------- maintenance (3.4) --
    @app.get("/api/maintenance")
    def maint_status():
        return {"state": maint.state, "sizes": maint.sizes(), "config": cfg.data.get("maintenance"),
                "backups": sorted(p.name for p in maint.dir.glob("*") if p.is_file())[-30:] if maint.dir.exists() else []}

    @app.post("/api/maintenance/run")
    def maint_run():
        return maint.run()

    # ------------------------------------------------- scaling + exits (3.2) --
    @app.get("/api/scaling")
    def scaling_status():
        return {**eng.scaler.snapshot(), "transfers_error": eng.transfers.error}

    @app.post("/api/scaling/demo")
    async def scaling_demo(request: Request):
        """Start the demo test balance ({"base": 10}) or switch it off ({"base": 0}). Demo environment only."""
        b = await request.json()
        try:
            base = float(b.get("base") or 0)
            if base > 0:
                eng.scaler.start_sim(base)
            else:
                eng.scaler.stop_sim()
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e))
        return eng.scaler.snapshot()

    @app.post("/api/scaling/recalc")
    def scaling_recalc():
        if not (cfg.data.get("scaling") or {}).get("enabled"):
            raise HTTPException(400, "scaling is off")
        if not eng.client.authenticated:
            raise HTTPException(400, "no working API key for this environment")
        eng.scaler.apply(force=True)
        return eng.scaler.snapshot()

    @app.get("/api/exits")
    def exits_list():
        return {"enabled": bool(cfg.data.get("exits", {}).get("enabled", True)), "rules": eng.exits.rules(active_only=False)[:100],
                "state": eng.exits.state}

    @app.post("/api/exits")
    async def exits_set(request: Request):
        b = await request.json()
        if not eng.client.authenticated:
            raise HTTPException(400, "no working API key for this environment")
        try:
            return eng.exits.set_rule(str(b.get("ticker") or ""), b.get("take_profit"), b.get("stop_loss"), b.get("floor"))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/exits/suggest/{ticker}")
    def exits_suggest(ticker: str):
        if not eng.client.authenticated:
            raise HTTPException(400, "no working API key for this environment")
        try:
            return eng.exits.suggest(ticker)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/exits/{ticker}")
    def exits_del(ticker: str):
        return {"removed": eng.exits.remove(ticker)}

    # ---------------------------------------------------- tax export (3) --
    @app.get("/api/export/tax")
    def tax_export(year: int = 0):
        from fastapi.responses import Response

        from . import taxes
        if not eng.client.authenticated:
            raise HTTPException(400, "no working API key for this environment")
        y = year or time.gmtime().tm_year
        if not 2020 <= y <= 2100:
            raise HTTPException(400, "bad year")
        text, tot = taxes.build(eng.client, y)
        db.audit("tax_export", f"{cfg.env} {y}: {json.dumps(tot)}")
        return Response(text, media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="kalshi-desk-{cfg.env}-{y}.csv"'})

    # ------------------------------------------------------- breakers (3) --
    @app.get("/api/breakers")
    def breakers_status():
        return eng.breakers.snapshot()

    @app.post("/api/breakers/check")
    def breakers_check():
        if not eng.client.authenticated:
            raise HTTPException(400, "no working API key for this environment")
        eng.breakers.check()
        return eng.breakers.snapshot()

    @app.post("/api/breakers/reset")
    async def breakers_reset(request: Request):
        b = await request.json()
        st = b.get("strategy")
        if st is not None and st not in eng.breakers.tripped():
            raise HTTPException(400, "that breaker is not tripped")
        eng.breakers.reset(st, peak=bool(b.get("peak")))
        return eng.breakers.snapshot()

    # ------------------------------------------------- market making (2.3) --
    # Paper-test reports are built in the background and served from the last finished build: building one waits on
    # the polling loop and reads a lot of history, which used to make this request (and the Home page) hang.
    mm_cache: dict = {"ts": 0.0, "r": None, "busy": None, "final": {}, "took": None}

    app.state.mm_cache = mm_cache
    app.state.studies = studies

    def _mm_build():
        t0 = time.time()
        try:
            live_ids = set(mm_shadow.runs)
            out = []
            for row in sorted(wdb.query("SELECT id, status FROM mm_runs ORDER BY id DESC LIMIT 40"), key=lambda r_: r_["id"]):
                i = row["id"]
                if i in live_ids:
                    x = mmod.report(wdb, i, live=mm_shadow)
                else:                               # finished runs never change: build their report once
                    x = mm_cache["final"].get(i)
                    if x is None:
                        x = mmod.report(wdb, i)
                        if x and row["status"] != "running":
                            mm_cache["final"][i] = x
                if x:
                    out.append(x)
            mm_cache.update(r=out, ts=time.time(), took=round(time.time() - t0, 1))
        except Exception as e:
            db.audit("mm_error", f"report build: {e}"[:300])
        finally:
            mm_cache["busy"] = None

    def _mm_refresh(wait: float = 2.0):
        th = mm_cache["busy"]
        if th is None:
            th = threading.Thread(target=_mm_build, daemon=True, name="mm-report")
            mm_cache["busy"] = th
            th.start()
        th.join(timeout=wait)                       # fast builds are returned fresh; slow ones finish in the background

    @app.get("/api/mm")
    def mm_status():
        sc = mm_scanner.latest()
        if time.time() - mm_cache["ts"] > 60 or mm_cache["r"] is None:
            _mm_refresh()
        reps = mm_cache["r"] or []
        live_reps = [x for x in reps if x.get("status") == "running"]
        return {"scan": sc, "scan_running": mm_scanner.running, "shadow": mm_shadow.status(), "reports": reps,
                "reports_pending": mm_cache["r"] is None, "reports_refreshing": mm_cache["busy"] is not None or mm_cache["ts"] == 0,
                "reports_age": round(time.time() - mm_cache["ts"]) if mm_cache["ts"] else None, "reports_took": mm_cache["took"],
                "report": (live_reps or reps)[-1] if reps else None,
                "runs": wdb.query("SELECT id, ts, status, stopped_ts, note, scan_id, params FROM mm_runs ORDER BY id DESC LIMIT 30")}

    @app.post("/api/mm/scan")
    def mm_scan():
        try:
            sid = mm_scanner.start({"quoter": {k: cfg.quoter.get(k) for k in ("pull_hours", "max_spread", "min_mid", "max_mid")},
                                    "include_all": (cfg.quoter.get("universe") or "all") == "all",
                                    "min_spread": cfg.quoter.get("min_spread"), "min_volume_24h": cfg.quoter.get("min_volume_24h")})
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return {"id": sid}

    @app.get("/api/mm/scan/rows")
    def mm_rows(size: int = 10, sort: str = "per_capital", limit: int = 100, all: int = 0, scan: int = 0):
        sc = mm_scanner.latest(done_only=True) if not scan else {"id": scan}
        if not sc:
            return {"rows": []}
        return {"scan_id": sc["id"], "rows": mm_scanner.rows(sc["id"], size, max(1, min(limit, 2000)),
                                                            sort if sort in ("per_capital", "daily", "pool") else "per_capital",
                                                            quoter_ok_only=not all)}

    def _mm_base(b: dict) -> dict:
        p = {"n_markets": max(1, min(40, int(b.get("n_markets") or 20))),
             "size": max(1, min(500, int(b.get("size") or cfg.quoter.get("size") or 10))),
             "poll_sec": max(5, min(120, int(b.get("poll_sec") or 15))),
             "max_days": max(1, min(60, float(b.get("max_days") or 21))),
             "per_series": max(1, min(40, int(b.get("per_series") or 3)))}
        for k in ("max_inventory", "pull_hours", "jump_cents", "cooldown_min", "refresh_sec", "min_mid", "max_mid", "max_spread"):
            p[k] = cfg.quoter.get(k)
        p["requeue_sec"] = max(60, float(cfg.quoter.get("quote_ttl_sec") or 600) - 60)   # same re-placement as the live quoter
        return p

    @app.post("/api/mm/shadow/start")
    async def mm_shadow_start(request: Request):
        b = await request.json()
        p = _mm_base(b)
        for k, lo, hi in (("offset_ticks", 0, 5), ("max_volume_24h", 0, 10 ** 9), ("pull_after_fill_min", 0, 240),
                          ("burst_contracts", 0, 10 ** 7), ("burst_pull_min", 1, 240)):
            if b.get(k) is not None:
                p[k] = max(lo, min(hi, float(b[k])))
        if b.get("exit_step_in"):
            p["exit_step_in"] = True
        if b.get("hold_to_settle"):
            p["hold_to_settle"] = True
        if b.get("min_quote_price") is not None:
            p["min_quote_price"] = max(0.0, min(0.99, float(b["min_quote_price"])))
        if isinstance(b.get("series_prefixes"), list):
            p["series_prefixes"] = [str(x)[:30] for x in b["series_prefixes"] if str(x).strip()][:20]
        for k, lo, hi in (("max_capital", 0, 10 ** 7), ("market_loss_stop", 0, 10 ** 6), ("max_move", 0, 1), ("max_inventory", 1, 10 ** 5)):
            if b.get(k) is not None:
                p[k] = max(lo, min(hi, float(b[k])))
        if b.get("select_by") == "trading":
            p["select_by"] = "trading"
            p["min_spread"] = max(0.01, min(0.2, float(b.get("min_spread") or 0.02)))
        if b.get("name"):
            p["name"] = str(b["name"])[:80]
        try:
            rid = mm_shadow.start(mm_scanner, p)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        mm_cache["ts"] = 0
        db.audit("mm_shadow_start", json.dumps(p))
        return {"id": rid, "params": p}

    @app.post("/api/mm/shadow/start_set")
    async def mm_shadow_start_set(request: Request):
        b = await request.json()
        p = _mm_base(b)
        try:
            ids = mm_shadow.start_set(mm_scanner, p)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        mm_cache["ts"] = 0
        skipped = getattr(mm_shadow, "last_skipped", [])
        db.audit("mm_shadow_start_set", json.dumps({"ids": ids, "skipped": skipped, **p}))
        return {"ids": ids, "skipped": skipped, "params": p}

    @app.post("/api/mm/shadow/verify_fees")
    def mm_shadow_verify_fees(apply: int = 1):
        out = mm_shadow.verify_fees(apply=bool(apply))
        mm_cache["ts"] = 0
        db.audit("mm_verify_fees", json.dumps({k: out[k] for k in ("series", "confirmed", "fees_added", "applied")} | {"changed": len(out["changed"])}))
        return out

    @app.post("/api/mm/shadow/stop")
    def mm_shadow_stop(run: int = 0):
        mm_shadow.stop(run or None)
        mm_cache["ts"] = 0
        db.audit("mm_shadow_stop", str(run or "all"))
        return {"ok": True}

    @app.get("/api/mm/report")
    def mm_report(run: int = 0):
        r = mmod.report(wdb, run or None, live=mm_shadow)
        if not r:
            raise HTTPException(404, "no shadow run yet")
        return r

    # -------------------------------------------------------------- quoter --
    @app.get("/api/quoter")
    def quoter():
        return {**eng.quoter.snapshot(), "config": cfg.quoter, "env": cfg.env}

    @app.get("/api/quoter/preview")
    def quoter_preview(env: str = "prod", size: int = 0):
        """Read-only dry run of market selection against public data. Places nothing."""
        if env not in BASE_URLS:
            raise HTTPException(400, "env must be demo or prod")
        q = eng.quoter
        old = cfg.quoter.get("size")
        try:
            if size > 0:
                cfg.quoter["size"] = size
            rows, msg = q.select(KalshiClient(BASE_URLS[env]))
        finally:
            cfg.quoter["size"] = old
        return {"env": env, "size": size or old, "selected": q._fmt(rows), "summary": msg}

    @app.get("/api/rewards")
    def rewards(days: int = 30):
        if not eng.client.authenticated:
            return {"authenticated": False}
        credits = eng.client.credit_history(min_ts=int(time.time()) - days * 86400)
        sp = eng.spent_today(max_age=0)
        pnl = eng.daily_pnl()
        q_orders = db.query("SELECT COUNT(*) n FROM orders_log WHERE env=? AND source='quoter' AND ts>=?",
                            (cfg.env, risk.start_of_today()))[0]["n"]
        return {"authenticated": True, "credits": credits,
                "credits_total": str(sum((d(c.get("amount_dollars")) for c in credits
                                          if "liquid" in (str(c.get("reason")) + str(c.get("type"))).lower()), d(0))),
                "other_credits_total": str(sum((d(c.get("amount_dollars")) for c in credits
                                                if "liquid" not in (str(c.get("reason")) + str(c.get("type"))).lower()), d(0))),
                "spent_today": {k: str(v) for k, v in sp.items()},
                "pnl_today": {k: str(v) for k, v in pnl.items()} if pnl else None,
                "max_daily_loss": cfg.risk.get("max_daily_loss"), "quoter_orders_today": q_orders}

    # -------------------------------------------------------- market data --
    @app.get("/api/categories")
    def categories():
        return eng.client.tags_by_category()

    @app.get("/api/events")
    def events(category: str = "", q: str = "", series_ticker: str = "", limit: int = 300):
        evs = eng.client.events(status="open", series_ticker=series_ticker or None, nested=True, max_items=3000)
        if category:
            evs = [e for e in evs if e.get("category") == category]
        if q:
            ql = q.lower()
            evs = [e for e in evs if ql in (e.get("title", "") + " " + e.get("sub_title", "") + " " +
                                           e.get("event_ticker", "")).lower()
                   or any(ql in (m.get("title", "") + m.get("yes_sub_title", "")).lower() for m in e.get("markets") or [])]
        def vol(e):
            return sum(float(m.get("volume_24h_fp") or 0) for m in e.get("markets") or [])
        evs.sort(key=vol, reverse=True)
        return {"events": evs[:limit], "total": len(evs)}

    @app.get("/api/feed")
    def feed_status():
        return eng.feed.status()

    def _book(ticker: str):
        eng.feed.want_ui(ticker)
        ob = eng.feed.book(ticker, depth=10)
        if ob is not None:
            return ob, "ws"
        return eng.client.orderbook(ticker, depth=10), "rest"

    @app.get("/api/book/{ticker}")
    def live_book(ticker: str):
        """Lightweight book refresh for the market view (live feed when available)."""
        ob, src = _book(ticker)
        book = book_from_orderbook(ob)
        js = lambda lv: [[str(p), str(c)] for p, c in lv]
        top = eng.feed.top(ticker)
        return {"ticker": ticker, "source": src, "book": {k: js(v) for k, v in book.items()},
                "top": {k: str(v) for k, v in top.items()} if top else None}

    @app.get("/api/market/{ticker}")
    def market(ticker: str):
        m = eng.feed.market_overlay(eng.client.market(ticker))
        ob, src = _book(ticker)
        book = book_from_orderbook(ob)
        ser = {}
        try:
            ev = eng.client.request("GET", f"/events/{m['event_ticker']}").get("event") or {}
            ser = eng.client.series(ev.get("series_ticker")) or {}
        except Exception:
            ev = {}
        fv = db.query("SELECT * FROM fair_values WHERE ticker=?", (ticker,))
        js = lambda lv: [[str(p), str(c)] for p, c in lv]
        return {"market": m, "book_source": src,
                "event": {k: ev.get(k) for k in ("title", "category", "mutually_exclusive", "series_ticker")},
                "fee_type": ser.get("fee_type"), "fee_multiplier": ser.get("fee_multiplier"),
                "book": {k: js(v) for k, v in book.items()}, "fair_value": fv[0] if fv else None}

    @app.get("/api/history/{ticker}")
    def history(ticker: str, days: int = 7):
        m = eng.client.market(ticker)
        ev = eng.client.request("GET", f"/events/{m['event_ticker']}").get("event") or {}
        end = int(time.time())
        period = 60 if days <= 14 else 1440
        return eng.client.candlesticks(ev["series_ticker"], ticker, end - days * 86400, end, period)

    @app.get("/api/fee")
    def fee_quote(price: str, count: str, fee_type: str = "quadratic", multiplier: str = "1"):
        return {"taker": str(fee(price, count, fee_type, multiplier)),
                "maker": str(fee(price, count, fee_type, multiplier, maker=True))}

    # ---------------------------------------------------------- portfolio --
    @app.get("/api/portfolio")
    def portfolio():
        c = eng.client
        if not c.authenticated:
            return {"authenticated": False}
        bal = c.balance()
        pos = eng.positions(max_age=0)
        tickers = [p["ticker"] for p in pos["market_positions"]]
        mk = {}
        for i in range(0, len(tickers), 100):
            for m in c.markets(status=None, tickers=tickers[i:i + 100], exclude_combos=False):
                mk[m["ticker"]] = m
        rows = []
        xr = {r["ticker"]: r for r in eng.exits.rules()}
        for p in pos["market_positions"]:
            m = mk.get(p["ticker"], {})
            qty = d(p.get("position_fp"))
            side = "YES" if qty > 0 else "NO"
            mark = d(m.get("yes_bid_dollars")) if qty > 0 else d(m.get("no_bid_dollars"))
            # what the position is worth: the bid for the side held; with no bid, the last trade (an estimate, flagged)
            last_yes = d(m.get("last_price_dollars"))
            last = last_yes if qty > 0 else (1 - last_yes if 0 < last_yes < 1 else d(0))
            if 0 < mark < 1:
                now_px, src = mark, "bid"
            elif 0 < last < 1:
                now_px, src = last, "last"
            else:
                now_px, src = None, "none"
            cost = d(p.get("market_exposure_dollars"))
            entry = (cost / abs(qty)).quantize(d("0.0001")) if qty != 0 and cost > 0 else None
            value = (abs(qty) * now_px).quantize(d("0.01")) if now_px is not None else None
            rows.append({**p, "title": m.get("title"), "sub": m.get("yes_sub_title"), "status": m.get("status"),
                         "side": side, "qty": str(abs(qty)), "mark": str(mark),
                         "mark_value": str((abs(qty) * mark).quantize(d("0.01"))),
                         "entry": str(entry) if entry is not None else None,
                         "now": str(now_px) if now_px is not None else None, "now_source": src,
                         "value_est": str(value) if value is not None else None,
                         "unrealized": str((value - cost).quantize(d("0.01"))) if value is not None and cost > 0 else None,
                         "close_time": m.get("close_time"), "exit": xr.get(p["ticker"])})
        return {"authenticated": True, "balance": bal, "positions": rows,
                "orders": c.orders(status="resting"), "fills": c.fills(max_items=200)}

    @app.get("/api/settlements")
    def settlements():
        if not eng.client.authenticated:
            return {"settlements": []}
        s = eng.client.settlements(max_items=1000)
        for r in s:
            db.execute("INSERT OR REPLACE INTO settlements VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (cfg.env, r.get("ticker"), r.get("event_ticker"), r.get("settled_time"), r.get("market_result"),
                        r.get("revenue"), r.get("yes_total_cost_dollars"), r.get("no_total_cost_dollars"),
                        r.get("fee_cost"), json.dumps(r)))
        return {"settlements": s}

    @app.get("/api/prod/history")
    def prod_history_view(days: float = 30, prefix: str = ""):
        """Results of the LIVE account, read-only (its fills and settlements only), while the desk stays on its current environment."""
        from .history import prod_history
        try:
            out = prod_history(cfg, days, prefix[:40])
        except Exception as e:
            db.audit("prod_history_error", str(e)[:300])
            raise HTTPException(502, f"could not read the production account: {e}")
        db.audit("prod_history_read", f"days={days} prefix={prefix[:40]} fills={out.get('fills_read')}")
        return out

    # ------------------------------------------------------------- orders --
    @app.post("/api/order")
    async def order(request: Request):
        b = await request.json()
        if cfg.env == "prod" and cfg.risk.get("require_confirm_prod"):
            tok = b.get("confirm_token")
            if not tok or tok not in prod_confirms or time.time() - prod_confirms.pop(tok) > 60:
                t = secrets.token_hex(8)
                prod_confirms[t] = time.time()
                return JSONResponse({"needs_confirm": True, "confirm_token": t,
                                     "message": "REAL MONEY. Confirm this order within 60 seconds."}, status_code=409)
        r = eng.place(ticker=b["ticker"], intent=b["intent"], price=b["price"], count=b["count"],
                      time_in_force=b.get("time_in_force", "good_till_canceled"),
                      post_only=bool(b.get("post_only")), expiration_time=b.get("expiration_time"))
        return r

    @app.delete("/api/order/{order_id}")
    def cancel(order_id: str, ticker: str = ""):
        r = eng.client.cancel_order(order_id, ticker or None)
        db.audit("cancel", f"{order_id} {ticker}")
        return r

    @app.post("/api/cancel_all")
    def cancel_all():
        n = 0
        for o in eng.client.orders(status="resting"):
            eng.client.cancel_order(o["order_id"], o.get("ticker"))
            n += 1
        db.audit("cancel_all", str(n))
        return {"cancelled": n}

    # ------------------------------------------------------------ scanner --
    @app.get("/api/signals")
    def signals(status: str = "new"):
        rows = db.query("SELECT * FROM signals WHERE env=? AND status=? ORDER BY actionable DESC, CAST(edge AS REAL) DESC LIMIT 300",
                        (cfg.env, status))
        for r in rows:
            r["legs"] = json.loads(r["legs"] or "[]")
            r["max_fit"] = eng.fit_size(r["legs"]) if r["legs"] else 0
        return {"signals": rows, "scan": eng.scan_state}

    @app.post("/api/scan")
    def scan():
        eng.trigger_scan()
        return {"ok": True}

    @app.post("/api/signals/{sid}/approve")
    async def approve(sid: int, request: Request):
        b = await request.json()
        rows = db.query("SELECT * FROM signals WHERE id=? AND env=?", (sid, cfg.env))
        if not rows:
            raise HTTPException(404, "signal not found")
        if not rows[0]["actionable"]:
            raise HTTPException(400, "informational signal - use the order ticket instead")
        if cfg.env == "prod" and cfg.risk.get("require_confirm_prod"):
            tok = b.get("confirm_token")
            if not tok or tok not in prod_confirms or time.time() - prod_confirms.pop(tok) > 60:
                t = secrets.token_hex(8)
                prod_confirms[t] = time.time()
                return JSONResponse({"needs_confirm": True, "confirm_token": t,
                                     "message": "REAL MONEY. Confirm within 60 seconds."}, status_code=409)
        return eng.execute_signal(rows[0], b.get("count"), source="approved")

    @app.post("/api/signals/{sid}/dismiss")
    def dismiss(sid: int):
        db.execute("UPDATE signals SET status='dismissed' WHERE id=?", (sid,))
        return {"ok": True}

    # -------------------------------------------------------- fair values --
    @app.get("/api/fair_values")
    def fair_values():
        return {"fair_values": db.query("SELECT * FROM fair_values ORDER BY ts DESC")}

    @app.post("/api/fair_values")
    async def set_fair_value(request: Request):
        b = await request.json()
        fv = d(b["fair_yes"])
        if not (d("0.01") <= fv <= d("0.99")):
            raise HTTPException(400, "fair_yes must be between 0.01 and 0.99")
        db.execute("INSERT OR REPLACE INTO fair_values VALUES(?,?,?,?)",
                   (b["ticker"], str(fv), b.get("note", ""), time.time()))
        return {"ok": True}

    @app.delete("/api/fair_values/{ticker}")
    def del_fair_value(ticker: str):
        db.execute("DELETE FROM fair_values WHERE ticker=?", (ticker,))
        return {"ok": True}

    # ------------------------------------------------------------ ledger --
    @app.get("/api/ledger")
    def ledger(quoter: int = 0, limit: int = 0, offset: int = 0, a_limit: int = 0, a_offset: int = 0, q: str = "", days: float = 0):
        """Order log and audit trail, newest first. Without parameters: the latest 500 orders / 200 audit lines (as before).
        limit/offset page the order log, a_limit/a_offset the audit; q searches both; days limits both to the last N days."""
        oc, oa = ([] if quoter else ["source!='quoter'"]), []
        ac, aa = [], []
        if days and days > 0:
            since = time.time() - float(days) * 86400
            oc.append("ts>=?"); oa.append(since)
            ac.append("ts>=?"); aa.append(since)
        if q.strip():
            like = f"%{q.strip()[:80]}%"
            oc.append("(ticker LIKE ? OR intent LIKE ? OR status LIKE ? OR source LIKE ? OR IFNULL(strategy,'') LIKE ? OR IFNULL(error,'') LIKE ?)")
            oa += [like] * 6
            ac.append("(kind LIKE ? OR message LIKE ?)")
            aa += [like] * 2
        ow = ("WHERE " + " AND ".join(oc)) if oc else ""
        aw = ("WHERE " + " AND ".join(ac)) if ac else ""
        lim, alim = max(1, min(limit or 500, 500)), max(1, min(a_limit or 200, 500))
        return {"orders": db.query("SELECT id,ts,env,source,strategy,ticker,intent,price,count,cost,status,"
                                   f"fill_count,avg_fill_price,avg_fee,error FROM orders_log {ow} ORDER BY id DESC LIMIT ? OFFSET ?",
                                   (*oa, lim, max(0, offset))),
                "orders_total": db.query(f"SELECT COUNT(*) n FROM orders_log {ow}", tuple(oa))[0]["n"],
                "audit": db.query(f"SELECT * FROM audit {aw} ORDER BY id DESC LIMIT ? OFFSET ?", (*aa, alim, max(0, a_offset))),
                "audit_total": db.query(f"SELECT COUNT(*) n FROM audit {aw}", tuple(aa))[0]["n"],
                "spent_today": str(eng.spent_today(max_age=0)["all"]) if eng.client.authenticated else "0"}

    return app
