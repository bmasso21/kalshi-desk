"""Remote access login (Phase 1.6).

The server only listens on 127.0.0.1. Remote access goes through `tailscale serve`, which proxies
https://<machine>.<tailnet>.ts.net to it, only for devices in your tailnet, and adds identity headers
(Tailscale-User-Login, ...) that it strips from incoming requests so they can't be spoofed.

Rules
  * LOCAL-DIRECT requests (loopback client, Host = localhost/127.0.0.1, no proxy or Tailscale headers)
    are trusted, exactly like v1.5 - anyone at the keyboard of this computer already controls it.
  * Every other request is REMOTE and needs a session from the login page (password).
    Remote is refused entirely until a password has been set (from this computer).
  * Optional: `allowed_tailscale_logins` - only those Tailscale accounts may even see the login page.
  * Password: PBKDF2-HMAC-SHA256, 600k iterations, random salt; min 12 characters.
  * Sessions: random 256-bit token in an HttpOnly, Secure, SameSite=Strict cookie; only its SHA-256 is
    stored; 30 days by default; "log out everywhere" available.
  * 5 wrong passwords in 15 minutes -> remote logins locked for 15 minutes + a phone alert.
    Every successful remote login also sends a phone alert.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

COOKIE = "kd_session"
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
PROXY_HEADERS = ("tailscale-user-login", "tailscale-user-name", "x-forwarded-for", "x-forwarded-host",
                 "x-forwarded-proto", "forwarded", "x-real-ip")
PUBLIC_PATHS = {"/login", "/api/auth/login", "/api/auth/status"}
ITER = 600_000
MIN_LEN = 12

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, created REAL, expires REAL, last_seen REAL, login TEXT, agent TEXT
);
"""


def hash_password(pw: str, salt: bytes | None = None, iterations: int = ITER) -> dict:
    salt = salt or secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, iterations)
    return {"password_hash": h.hex(), "salt": salt.hex(), "iterations": iterations}


def verify_password(pw: str, rec: dict) -> bool:
    if not rec.get("password_hash"):
        return False
    h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(rec["salt"]), int(rec.get("iterations", ITER)))
    return hmac.compare_digest(h.hex(), rec["password_hash"])


class Auth:
    def __init__(self, cfg, db, alerts=None, extra_local_hosts=()):
        self.cfg, self.db, self.alerts = cfg, db, alerts
        self.local_hosts = LOOPBACK | set(extra_local_hosts)
        self.db.conn.executescript(SCHEMA)
        self.failures: list[float] = []
        self.locked_until = 0.0

    @property
    def conf(self) -> dict:
        return self.cfg.data.get("auth", {})

    @property
    def password_set(self) -> bool:
        return bool(self.conf.get("password_hash"))

    # ----------------------------------------------------------- classify --
    def is_local(self, request) -> bool:
        client = request.client.host if request.client else ""
        if client not in self.local_hosts:
            return False
        if any(h in request.headers for h in PROXY_HEADERS):
            return False
        host = (request.headers.get("host") or "").split(":")[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1", "testserver")

    def ts_login(self, request) -> str | None:
        return request.headers.get("tailscale-user-login")

    # ------------------------------------------------------------ session --
    def session(self, request) -> dict | None:
        tok = request.cookies.get(COOKIE)
        if not tok:
            return None
        th = hashlib.sha256(tok.encode()).hexdigest()
        rows = self.db.query("SELECT * FROM sessions WHERE token_hash=?", (th,))
        if not rows or rows[0]["expires"] < time.time():
            return None
        if time.time() - (rows[0]["last_seen"] or 0) > 300:
            self.db.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (time.time(), th))
        return rows[0]

    def check(self, request) -> tuple[bool, int, str]:
        """(allowed, status_if_not, message)"""
        if self.is_local(request):
            return True, 0, ""
        allowed = [x.lower() for x in self.conf.get("allowed_tailscale_logins") or []]
        login = (self.ts_login(request) or "").lower()
        if allowed and login not in allowed:
            return False, 403, "This Tailscale account is not allowed to use Kalshi Desk."
        if not self.password_set:
            return False, 403, "Remote access is off until a password is set (Settings, on the server computer)."
        if request.url.path in PUBLIC_PATHS:
            return True, 0, ""
        if self.session(request):
            return True, 0, ""
        return False, 401, "Please log in."

    # -------------------------------------------------------------- login --
    def login(self, request, password: str) -> tuple[bool, str, str | None]:
        now = time.time()
        if now < self.locked_until:
            return False, f"Too many wrong passwords. Try again in {int((self.locked_until - now) / 60) + 1} min.", None
        self.failures = [t for t in self.failures if now - t < 900]
        if not verify_password(password or "", self.conf):
            self.failures.append(now)
            if len(self.failures) >= 5:
                self.locked_until = now + 900
                self.failures = []
                self.db.audit("auth_lockout", f"5 wrong passwords; remote login locked 15 min (from {self.ts_login(request)})")
                if self.alerts:
                    self.alerts.send("auth", "Kalshi Desk: login locked",
                                     f"5 wrong passwords from {self.ts_login(request) or 'unknown'}. Remote login locked for 15 min.", 1)
            return False, "Wrong password.", None
        self.failures = []
        tok = secrets.token_urlsafe(32)
        days = float(self.conf.get("session_days", 30))
        self.db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)",
                        (hashlib.sha256(tok.encode()).hexdigest(), now, now + days * 86400, now,
                         self.ts_login(request), (request.headers.get("user-agent") or "")[:200]))
        self.db.audit("auth_login", f"remote login {self.ts_login(request)}")
        if self.alerts and not self.is_local(request):
            self.alerts.send("auth", "Kalshi Desk: new login",
                             f"Logged in from {self.ts_login(request) or 'a tailnet device'} "
                             f"({(request.headers.get('user-agent') or '')[:80]}).", -1)
        return True, "ok", tok

    def logout(self, request, everywhere=False):
        if everywhere:
            self.db.execute("DELETE FROM sessions")
        else:
            tok = request.cookies.get(COOKIE)
            if tok:
                self.db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(tok.encode()).hexdigest(),))

    def set_password(self, request, new: str, current: str | None) -> tuple[bool, str]:
        if len(new or "") < MIN_LEN:
            return False, f"Password must be at least {MIN_LEN} characters."
        if self.password_set and not self.is_local(request) and not verify_password(current or "", self.conf):
            return False, "Current password is wrong."
        if not self.password_set and not self.is_local(request):
            return False, "Set the first password on the server computer."
        self.cfg.update({"auth": hash_password(new)})
        self.db.execute("DELETE FROM sessions")         # changing the password logs everyone out
        self.db.audit("auth_password", "password set/changed; all sessions logged out")
        return True, "Password saved. All sessions were logged out."

    def sessions(self) -> list[dict]:
        return self.db.query("SELECT created, expires, last_seen, login, agent FROM sessions WHERE expires>? "
                             "ORDER BY last_seen DESC", (time.time(),))
