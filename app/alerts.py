"""Phone alerts via Pushover (https://pushover.net/api).

Events and priorities
  fills              normal (0)     batched: fills within `fill_batch_sec` go out as one message
  kill switch        emergency (2)  repeats until acknowledged; manual kill from the UI is high (1)
  leg risk           emergency (2)
  errors             high (1)       throttled per kind (`error_throttle_min`)
  feed down          high (1)       only if the WebSocket stays down `feed_down_min` minutes
  app started        normal / high  high when the previous run did not shut down cleanly (crash, power loss)
  app down           emergency (2)  sent by the separate watchdog process (watchdog.py)
  daily summary      low (-1)       once a day at `summary_time` (local)

Optional dead-man switch: if `healthcheck_url` is set (e.g. a free healthchecks.io check with its
Pushover integration), the app pings it every minute; if A-NEXIS loses power or internet the
pings stop and healthchecks.io alerts you. Nothing running on this computer can do that itself.

Credentials (Pushover user key + application token) are entered in Settings and never sent to the
browser. Every alert attempt is recorded in the `alerts` table.
"""
from __future__ import annotations

import datetime as dt
import json
import queue
import threading
import time
import traceback
from decimal import Decimal

import requests

from .kalshi_client import d

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
LOW, NORMAL, HIGH, EMERGENCY = -1, 0, 1, 2

ERROR_KINDS = {"connect_error", "scan_error", "auto_error", "loss_check_error", "fill_sync_error",
               "feed_watch_error", "api_error", "quoter_error", "summary_error", "watchdog_error", "clv_error", "warehouse_error", "breaker_error"}


class Alerter:
    def __init__(self, engine):
        self.e = engine
        self.q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._fills: list[dict] = []
        self._fills_first = 0.0
        self._last_err: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}
        self._net_hist: dict[str, list] = {}
        self._feed_down_since: float | None = None
        self._feed_down_sent = False
        self._last_hc = 0.0
        self.session = requests.Session()
        self.clean_flag = engine.cfg.path.parent / "logs" / "clean_shutdown.flag"

    # ------------------------------------------------------------ config --
    @property
    def cfg(self) -> dict:
        return self.e.cfg.data.get("alerts", {})

    def configured(self) -> bool:
        c = self.cfg
        return bool(c.get("enabled") and c.get("pushover_user") and c.get("pushover_token"))

    def api_url(self) -> str:
        return self.cfg.get("api_url_override") or PUSHOVER_URL

    # ------------------------------------------------------------ public --
    def send(self, kind: str, title: str, message: str, priority: int = NORMAL, sync: bool = False) -> dict:
        item = {"kind": kind, "title": title[:250], "message": message[:1024], "priority": priority}
        if sync:
            return self._deliver(item)
        self.q.put(item)
        return {"queued": True}

    def on_fill(self, f: dict):
        """A NEW fill (already de-duplicated against the fills table)."""
        if self.cfg.get("fills", True):
            self._queue_fill(f)

    def on_feed(self, kind: str, msg: dict):
        if kind == "feed_down":
            if self._feed_down_since is None:
                self._feed_down_since = time.time()
        elif kind == "feed_up":
            if self._feed_down_sent:
                mins = (time.time() - (self._feed_down_since or time.time())) / 60
                self.send("feed_up", "Live feed restored", f"WebSocket reconnected after {mins:.0f} min.", NORMAL)
            self._feed_down_since, self._feed_down_sent = None, False

    def on_audit(self, kind: str, message: str):
        c = self.cfg
        if kind == "KILL" and c.get("kill", True):
            manual = message.startswith("manual kill switch")
            self.send("kill", "KILL SWITCH ON" + ("" if manual else " - tripped automatically"),
                      f"{message}\nAll new orders blocked, auto + quoter off. Env: {self.e.cfg.env.upper()}",
                      HIGH if manual else EMERGENCY)
        elif kind == "BREAKER" and c.get("kill", True):
            self.send("breaker", "Circuit breaker tripped", f"{message}\nEnv: {self.e.cfg.env.upper()}", HIGH)
        elif kind == "LEG_RISK" and c.get("kill", True):
            self.send("leg_risk", "LEG RISK - partial basket", f"{message}\nCheck positions now.", EMERGENCY)
        elif kind in ERROR_KINDS and c.get("errors", True):
            now = time.time()
            if self.is_network(message):
                # brief network blips are normal; page only if they persist
                h = self._net_hist.setdefault(kind, [])
                h[:] = [t for t in h if now - t < 900] + [now]
                if len(h) < int(c.get("network_errors_to_alert", 3)):
                    return
                message = f"{len(h)} network failures in 15 min. Latest: {message}"
            thr = float(c.get("error_throttle_min", 30)) * 60
            if now - self._last_err.get(kind, 0) < thr:
                self._suppressed[kind] = self._suppressed.get(kind, 0) + 1
                return
            extra = self._suppressed.pop(kind, 0)
            self._last_err[kind] = now
            more = f"\n(+{extra} more since last alert)" if extra else ""
            self.send("error", f"Error: {kind}", f"{message[-700:]}{more}", HIGH)

    NET_MARKERS = ("ConnectionError", "Connection aborted", "RemoteDisconnected", "Read timed out",
                   "ConnectTimeout", "ReadTimeout", "Max retries exceeded", "NameResolutionError",
                   "Kalshi API 500", "Kalshi API 502", "Kalshi API 503", "Kalshi API 504", "Kalshi API 429")

    @classmethod
    def is_network(cls, message: str) -> bool:
        return any(m in (message or "") for m in cls.NET_MARKERS)

    # -------------------------------------------------------------- fills --
    def _queue_fill(self, f: dict):
        if not self._fills:
            self._fills_first = time.time()
        self._fills.append(f)

    @staticmethod
    def fill_line(f: dict) -> str:
        t = f.get("market_ticker") or f.get("ticker") or "?"
        side = (f.get("purchased_side") or f.get("outcome_side") or f.get("side") or "").upper()
        act = (f.get("action") or "").upper() or ("BUY" if f.get("book_side") == "bid" else "")
        yp = d(f.get("yes_price_dollars"))
        px = (Decimal(1) - yp) if side == "NO" and yp else yp
        cnt = d(f.get("count_fp") or f.get("count"))
        fee = f.get("fee_cost") if f.get("fee_cost") is not None else f.get("fee_cost_dollars")
        role = "taker" if f.get("is_taker") else "maker"
        s = f"{act} {side} {cnt.normalize():f} x {t} @ ${px:.2f}"
        if fee not in (None, ""):
            s += f" (fee ${d(fee):.2f}, {role})"
        return s

    def _flush_fills(self, force=False):
        if not self._fills:
            return
        if not force and time.time() - self._fills_first < float(self.cfg.get("fill_batch_sec", 10)):
            return
        fs, self._fills = self._fills, []
        env = self.e.cfg.env.upper()
        if len(fs) == 1:
            self.send("fill", f"Fill ({env})", self.fill_line(fs[0]), NORMAL)
        else:
            lines = [self.fill_line(f) for f in fs[:12]]
            if len(fs) > 12:
                lines.append(f"... and {len(fs) - 12} more")
            self.send("fill", f"{len(fs)} fills ({env})", "\n".join(lines), NORMAL)

    # ------------------------------------------------------- lifecycle --
    def startup(self, version: str):
        """Called once when the server starts. Detects an unclean previous stop."""
        clean = self.clean_flag.exists()
        reason = ""
        try:
            reason = self.clean_flag.read_text("utf-8").strip() if clean else ""
            self.clean_flag.unlink(missing_ok=True)
        except OSError:
            pass
        first = not self.e.db.query("SELECT 1 FROM alerts LIMIT 1")
        if not self.cfg.get("lifecycle", True):
            return
        if clean or first:
            self.send("started", "Kalshi Desk started",
                      f"v{version} running, env {self.e.cfg.env.upper()}."
                      + (f" Previous stop: {reason}." if reason else ""),
                      LOW if "update" in reason else NORMAL)   # routine update restarts are silent
        else:
            self.send("started", "Kalshi Desk restarted after an unclean stop",
                      f"v{version} running, env {self.e.cfg.env.upper()}. The previous run ended without a clean "
                      "shutdown (crash, power loss, forced reboot, or Windows Update).", HIGH)

    def mark_clean(self, reason: str):
        try:
            self.clean_flag.parent.mkdir(parents=True, exist_ok=True)
            self.clean_flag.write_text(reason, "utf-8")
        except OSError:
            pass

    # ------------------------------------------------------ daily summary --
    def summary_text(self) -> str:
        e, db, env = self.e, self.e.db, self.e.cfg.env
        from . import risk
        t0 = risk.start_of_today()
        lines = [f"{dt.date.today():%a %b %d} · {env.upper()}"]
        try:
            p = e.daily_pnl()
            if p:
                lines.append(f"P&L today: {'+' if p['pnl'] >= 0 else '-'}${abs(p['pnl']):.2f} "
                             f"(equity ${p['start']:.2f} -> ${p['now']:.2f})")
            else:
                lines.append("P&L: no API key for this environment")
        except Exception as ex:
            lines.append(f"P&L: unavailable ({ex})")
        f = db.query("SELECT COUNT(*) n, COALESCE(SUM(CAST(count AS REAL)),0) c, COALESCE(SUM(CAST(fee AS REAL)),0) fee "
                     "FROM fills WHERE env=? AND ts>=?", (env, t0))[0]
        lines.append(f"Fills: {f['n']} ({f['c']:.0f} contracts), fees ${f['fee']:.2f}")
        by = db.query("SELECT COALESCE(order_source,'outside') s, COUNT(*) n FROM fills WHERE env=? AND ts>=? "
                      "GROUP BY s ORDER BY n DESC", (env, t0))
        if by:
            lines.append("  by source: " + ", ".join(f"{r['s']} {r['n']}" for r in by))
        o = db.query("SELECT COUNT(*) n, SUM(status='blocked') b, SUM(status='rejected') r FROM orders_log "
                     "WHERE env=? AND ts>=?", (env, t0))[0]
        lines.append(f"Orders: {o['n'] or 0} attempted, {o['b'] or 0} blocked by risk, {o['r'] or 0} rejected")
        try:
            sp = e.spent_today(max_age=0)
            lines.append(f"Filled buys: ${sp['all']:.2f} (auto/quoter ${sp['auto']:.2f})")
        except Exception:
            pass
        try:
            cs = e.clv.summary(30)["overall"]
            if cs["contracts"]:
                lines.append(f"CLV 30d: {cs['avg_clv_cents']:+.2f}c/contract over {cs['contracts']:.0f} contracts, "
                             f"beat the close {cs['beat_close_pct']:.0f}%")
        except Exception:
            pass
        errs = db.query("SELECT kind, COUNT(*) n FROM audit WHERE ts>=? AND (kind LIKE '%error%' OR kind IN "
                        "('KILL','LEG_RISK','feed_down')) GROUP BY kind", (t0,))
        lines.append("Issues: " + (", ".join(f"{r['kind']} x{r['n']}" for r in errs) if errs else "none"))
        fs = e.feed.status()
        lines.append(f"Feed: {fs['mode']}, {fs['stats']['reconnects']} reconnects since start")
        c = e.cfg
        lines.append(f"Kill switch {'ON' if c.risk.get('kill_switch') else 'off'} · auto "
                     f"{'ON' if c.auto.get('enabled') else 'off'} · quoter {'ON' if c.quoter.get('enabled') else 'off'}")
        return "\n".join(lines)

    def _maybe_summary(self):
        c = self.cfg
        if not c.get("daily_summary", True):
            return
        try:
            hh, mm = (int(x) for x in str(c.get("summary_time", "23:50")).split(":"))
        except ValueError:
            hh, mm = 23, 50
        now = dt.datetime.now()
        if (now.hour, now.minute) < (hh, mm):
            return
        day = now.date().isoformat()
        if self.e.db.query("SELECT 1 FROM alerts WHERE kind='summary' AND day=? AND status IN ('sent','skipped')", (day,)):
            return
        try:
            text = self.summary_text()
        except Exception:
            self.e.db.audit("summary_error", traceback.format_exc()[-600:])
            return
        self._deliver({"kind": "summary", "title": f"Kalshi Desk daily summary", "message": text, "priority": LOW})

    # -------------------------------------------------------- transport --
    def _deliver(self, item: dict) -> dict:
        c = self.cfg
        day = dt.date.today().isoformat()
        if not self.configured():
            self._record(item, "skipped", "alerts not configured", day)
            return {"ok": False, "status": "skipped", "error": "Pushover not configured or alerts disabled"}
        data = {"token": c["pushover_token"], "user": c["pushover_user"], "title": item["title"],
                "message": item["message"], "priority": item["priority"]}
        if c.get("device"):
            data["device"] = c["device"]
        if item["priority"] == EMERGENCY:
            data["retry"] = max(30, int(c.get("emergency_retry", 60)))
            data["expire"] = min(10800, int(c.get("emergency_expire", 3600)))
        if item["priority"] >= HIGH and c.get("sound_urgent"):
            data["sound"] = c["sound_urgent"]
        err = None
        for attempt in range(3):
            try:
                r = self.session.post(self.api_url(), data=data, timeout=15)
                body = r.json() if r.content else {}
                if r.status_code == 200 and body.get("status") == 1:
                    self._record(item, "sent", body.get("receipt") or body.get("request"), day)
                    return {"ok": True, "status": "sent", "receipt": body.get("receipt")}
                err = f"HTTP {r.status_code}: {body.get('errors') or body}"
                if 400 <= r.status_code < 500:
                    break   # bad key / token / params: retrying won't help
            except Exception as ex:
                err = f"{type(ex).__name__}: {ex}"
            time.sleep(2 * (attempt + 1))
        self._record(item, "failed", err, day)
        return {"ok": False, "status": "failed", "error": err}

    def _record(self, item, status, detail, day):
        self.e.db.execute("INSERT INTO alerts(ts,day,kind,priority,title,message,status,detail) VALUES(?,?,?,?,?,?,?,?)",
                          (time.time(), day, item["kind"], item["priority"], item["title"], item["message"],
                           status, str(detail or "")[:500]))

    def drain(self):
        """Deliver everything queued right now (used by stop() and tests)."""
        while True:
            try:
                self._deliver(self.q.get_nowait())
            except queue.Empty:
                return

    # ------------------------------------------------------------- loop --
    def tick(self):
        """One maintenance pass (also called directly by tests)."""
        self._flush_fills()
        if self._feed_down_since and not self._feed_down_sent and self.e.feed.state.get("enabled"):
            mins = float(self.cfg.get("feed_down_min", 5))
            if time.time() - self._feed_down_since >= mins * 60 and not self.e.feed.live():
                self._feed_down_sent = True
                self.send("feed_down", "Live feed down",
                          f"WebSocket down for {mins:.0f}+ min ({self.e.feed.state.get('error')}). "
                          "Running on REST polling.", HIGH)
        self._maybe_summary()
        url = (self.cfg.get("healthcheck_url") or "").strip()
        if url and time.time() - self._last_hc >= 60:
            self._last_hc = time.time()
            try:
                self.session.get(url, timeout=10)
            except Exception:
                pass

    def loop(self):
        while not self._stop.is_set():
            try:
                item = self.q.get(timeout=1.0)
                self._deliver(item)
            except queue.Empty:
                pass
            except Exception:
                pass
            try:
                self.tick()
            except Exception:
                pass

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="alerts").start()

    def stop(self, reason="clean shutdown"):
        self._flush_fills(force=True)
        deadline = time.time() + 5
        while not self.q.empty() and time.time() < deadline:
            try:
                self._deliver(self.q.get_nowait())
            except queue.Empty:
                break
        self._stop.set()
        self.mark_clean(reason)
