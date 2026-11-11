"""Watchdog: a separate process that checks the desk every 30s.

* App unreachable for `app_down_min` minutes -> EMERGENCY Pushover alert ("Kalshi Desk is DOWN").
* Still down 3 minutes after that -> restarts the KalshiDesk scheduled task (end + run) and says so.
* Back up -> normal alert with the outage length.
A normal update restart (restart.request, ~20s) stays under the threshold and alerts nothing.

Started by run.py as a detached process; it writes logs/watchdog.heartbeat every cycle and the app
starts a new one if that heartbeat goes stale. Reads alert settings from config.json each cycle.
Run directly:  python -m app.watchdog
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
INTERVAL = 30


class Watchdog:
    def __init__(self, config_path: Path, fetch=None, restart=None, now=time.time, post=None):
        self.config_path = Path(config_path)
        self.fetch = fetch or self._fetch
        self.restart = restart or self._restart_task
        self.now = now
        self.post = post or (lambda url, data: requests.post(url, data=data, timeout=15))
        self.down_since: float | None = None
        self.alerted = False
        self.restarted_at: float | None = None
        self.log_path = self.config_path.parent / "logs" / "watchdog.log"
        self.heartbeat = self.config_path.parent / "logs" / "watchdog.heartbeat"

    # ------------------------------------------------------------ helpers --
    def cfg(self) -> dict:
        try:
            return json.loads(self.config_path.read_text("utf-8"))
        except Exception:
            return {}

    def log(self, msg):
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
        except OSError:
            pass

    def _fetch(self, port) -> bool:
        try:
            r = requests.get(f"http://127.0.0.1:{port}/api/session", timeout=10)
            return r.status_code == 200
        except Exception:
            return False

    def _restart_task(self):
        """Kill a hung server (the boot task's launcher loop restarts it in ~15s). If the launcher
        itself isn't running, start the task. Never ends the task, so the watchdog survives."""
        if os.name != "nt":
            return "not windows"
        ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like "
              "'*KalshiDesk*run.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=60)
        killed = " ".join((r.stdout or "").split())
        q = subprocess.run(["schtasks", "/query", "/tn", "KalshiDesk", "/fo", "csv", "/nh"],
                           capture_output=True, text=True, timeout=60)
        out = f"killed [{killed or 'none'}]"
        if "Running" not in (q.stdout or ""):
            r2 = subprocess.run(["schtasks", "/run", "/tn", "KalshiDesk"], capture_output=True, text=True, timeout=60)
            out += f"; task was not running -> {(r2.stdout or r2.stderr).strip()}"
        else:
            out += "; launcher running, it restarts the server in ~15s"
        return out

    def alert(self, title, message, priority):
        c = self.cfg()
        a = c.get("alerts", {})
        if not (a.get("enabled", True) and a.get("lifecycle", True) and a.get("pushover_user") and a.get("pushover_token")):
            self.log(f"(alert not sent - not configured) {title}: {message}")
            return False
        data = {"token": a["pushover_token"], "user": a["pushover_user"], "title": title, "message": message,
                "priority": priority}
        if a.get("device"):
            data["device"] = a["device"]
        if priority == 2:
            data["retry"] = max(30, int(a.get("emergency_retry", 60)))
            data["expire"] = min(10800, int(a.get("emergency_expire", 3600)))
        try:
            r = self.post(a.get("api_url_override") or PUSHOVER_URL, data)
            ok = getattr(r, "status_code", 0) == 200
        except Exception as e:
            ok = False
            self.log(f"alert error: {e}")
        self.log(f"alert {'sent' if ok else 'FAILED'}: {title}")
        return ok

    # --------------------------------------------------------------- step --
    def step(self):
        c = self.cfg()
        port = int(c.get("port", 8765))
        a = c.get("alerts", {})
        thr = float(a.get("app_down_min", 2)) * 60
        now = self.now()
        try:
            self.heartbeat.parent.mkdir(parents=True, exist_ok=True)
            self.heartbeat.write_text(str(now), "utf-8")
        except OSError:
            pass
        up = self.fetch(port)
        if up:
            if self.down_since is not None:
                mins = (now - self.down_since) / 60
                if self.alerted:
                    self.alert("Kalshi Desk is back up", f"Reachable again after {mins:.1f} min down.", 0)
                self.log(f"up again after {mins:.1f} min")
            self.down_since, self.alerted, self.restarted_at = None, False, None
            return "up"
        if self.down_since is None:
            self.down_since = now
            self.log("unreachable")
        down = now - self.down_since
        if down >= thr and not self.alerted:
            self.alerted = True
            self.alert("Kalshi Desk is DOWN",
                       f"Not responding on 127.0.0.1:{port} for {down / 60:.1f} min. "
                       "The watchdog will restart it in 3 min if it doesn't come back.", 2)
        if self.alerted and self.restarted_at is None and down >= thr + 180:
            self.restarted_at = now
            res = self.restart()
            self.log(f"restart attempted: {res}")
            self.alert("Kalshi Desk restart attempted", f"Watchdog restarted the boot task: {str(res)[:300]}", 1)
        elif self.restarted_at and now - self.restarted_at >= 600:
            self.restarted_at = None   # allow another restart attempt every 10 min while down
        return "down"

    def run(self):
        self.log(f"watchdog started pid={os.getpid()}")
        while True:
            try:
                self.step()
            except Exception as e:
                self.log(f"step error: {e}")
            time.sleep(INTERVAL)


def heartbeat_fresh(root: Path = ROOT, max_age: float = 120) -> bool:
    hb = root / "logs" / "watchdog.heartbeat"
    try:
        return time.time() - float(hb.read_text("utf-8")) < max_age
    except Exception:
        return False


def spawn(root: Path = ROOT):
    """Start a detached watchdog unless one is already alive (fresh heartbeat)."""
    if heartbeat_fresh(root):
        return False
    args = [sys.executable, "-m", "app.watchdog"]
    if os.name == "nt":
        DETACHED, NEW_GROUP, NO_WINDOW, BREAKAWAY = 0x8, 0x200, 0x08000000, 0x01000000
        try:   # leave the scheduled task's job so ending the task doesn't kill the watchdog
            subprocess.Popen(args, cwd=str(root), creationflags=DETACHED | NEW_GROUP | NO_WINDOW | BREAKAWAY,
                             close_fds=True)
        except OSError:
            subprocess.Popen(args, cwd=str(root), creationflags=DETACHED | NEW_GROUP | NO_WINDOW, close_fds=True)
    else:
        subprocess.Popen(args, cwd=str(root), start_new_session=True, close_fds=True)
    return True


if __name__ == "__main__":
    cfgp = Path(os.environ.get("KALSHI_DESK_CONFIG", ROOT / "config.json"))
    Watchdog(cfgp).run()
