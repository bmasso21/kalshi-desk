"""Phase 1.6 tests: remote access login (Tailscale serve + password)."""
import json
import sys
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK, FAIL = [], []
ME, OTHER = "brian@example.com", "stranger@example.com"
REMOTE = {"Tailscale-User-Login": ME, "Tailscale-User-Name": "Brian", "X-Forwarded-For": "100.101.102.103"}
PW = "correct horse battery"


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def main():
    from app.config import Config
    from app.db import DB
    from app.server import create_app

    tmp = tempfile.mkdtemp()
    cfgp = Path(tmp) / "config.json"
    cfgp.write_text(json.dumps({"env": "demo", "base_url_override": "http://127.0.0.1:9/trade-api/v2", "ws_enabled": False}))
    cfg = Config(cfgp)
    db = DB(Path(tmp) / "t.db")
    app = create_app(cfg, db, start_engine=False)
    auth, eng = app.state.auth, app.state.engine
    local = TestClient(app)
    LT = {"X-Desk-Token": local.get("/api/session").json()["token"]}
    remote = TestClient(app, base_url="https://testserver")

    # ---------------- before a password exists ---------------------------------
    check("local works with no password (unchanged from 1.5)", local.get("/api/config").status_code == 200)
    r = remote.get("/api/config", headers=REMOTE)
    check("remote refused until a password is set", r.status_code == 403 and "password" in r.json()["error"], r.text)
    r = remote.get("/", headers=REMOTE)
    check("remote page shows the refusal (no app)", r.status_code == 403 and "Kalshi Desk" in r.text and "<script" not in r.text)
    check("remote can't fetch the CSRF token", remote.get("/api/session", headers=REMOTE).status_code == 403)
    r = remote.post("/api/auth/password", headers=REMOTE, json={"new": PW})
    check("remote can't set the first password", r.status_code in (400, 403) and not auth.password_set, r.text)

    # spoof attempts: loopback client but proxy header / foreign Host -> remote
    check("X-Forwarded-For alone makes it remote", local.get("/api/config", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 403)
    check("foreign Host header makes it remote", local.get("/api/config", headers={"Host": "a-nexis.tail1234.ts.net"}).status_code == 403)
    check("Tailscale identity header alone makes it remote", local.get("/api/config", headers={"Tailscale-User-Login": ME}).status_code == 403)

    # ---------------- set password locally ---------------------------------------
    r = local.post("/api/auth/password", headers=LT, json={"new": "short"})
    check("password under 12 chars rejected", r.status_code == 400 and "12" in r.json()["error"])
    r = local.post("/api/auth/password", headers=LT, json={"new": PW})
    check("local sets password", r.status_code == 200 and auth.password_set, r.text)
    raw = cfgp.read_text()
    check("only a PBKDF2 hash is stored (no plaintext)", PW not in raw and len(cfg.data["auth"]["password_hash"]) == 64
          and cfg.data["auth"]["iterations"] == 600000)
    pub = local.get("/api/config").json()["config"]["auth"]
    check("config view hides hash + salt", "password_hash" not in pub and "salt" not in pub and pub["password_set"] is True, pub)
    local.post("/api/config", headers=LT, json={"auth": {"password_hash": "00" * 32, "salt": "00"}})
    check("hash can't be overwritten through /api/config", cfg.data["auth"]["password_hash"] != "00" * 32)

    # ---------------- remote login flow ------------------------------------------
    r = remote.get("/", headers=REMOTE, follow_redirects=False)
    check("remote, not logged in: / redirects to /login", r.status_code == 303 and r.headers["location"] == "/login")
    check("login page served", remote.get("/login", headers=REMOTE).status_code == 200)
    r = remote.get("/api/status", headers=REMOTE)
    check("API says 401 + login flag", r.status_code == 401 and r.json()["login"] is True)
    st = remote.get("/api/auth/status", headers=REMOTE).json()
    check("status endpoint public, reports Tailscale identity", st["tailscale_login"] == ME and not st["local"] and not st["logged_in"])
    for _ in range(3):
        r = remote.post("/api/auth/login", headers=REMOTE, json={"password": "wrong"})
    check("wrong password -> 401", r.status_code == 401 and r.json()["error"] == "Wrong password.")
    r = remote.post("/api/auth/login", headers=REMOTE, json={"password": PW})
    sc = r.headers.get("set-cookie", "")
    check("correct password -> session cookie", r.status_code == 200 and "kd_session=" in sc, r.text)
    check("cookie is HttpOnly + Secure + SameSite=Strict", "httponly" in sc.lower() and "secure" in sc.lower()
          and "samesite=strict" in sc.lower(), sc)
    tok = remote.cookies.get("kd_session")
    rows = db.query("SELECT token_hash, login FROM sessions")
    check("session stored only as a hash, with the Tailscale login", len(rows) == 1 and rows[0]["token_hash"] != tok
          and rows[0]["login"] == ME)
    check("logged in: API works", remote.get("/api/status", headers=REMOTE).status_code == 200)
    tokr = remote.get("/api/session", headers=REMOTE).json()["token"]
    check("POST still needs the CSRF token", remote.post("/api/scan", headers=REMOTE).status_code == 403
          and remote.post("/api/scan", headers={**REMOTE, "X-Desk-Token": tokr}).status_code == 200)
    eng.alerts.drain()
    al = db.query("SELECT title, priority FROM alerts WHERE kind='auth'")
    check("successful remote login -> silent phone alert", any("new login" in a["title"] and a["priority"] == -1 for a in al), al)

    # ---------------- lockout ------------------------------------------------------
    other = TestClient(app, base_url="https://testserver")
    for _ in range(5):
        other.post("/api/auth/login", headers=REMOTE, json={"password": "nope"})
    r = other.post("/api/auth/login", headers=REMOTE, json={"password": PW})
    check("5 wrong passwords -> locked, even the right one", r.status_code == 401 and "Too many" in r.json()["error"], r.text)
    eng.alerts.drain()
    check("lockout -> high-priority phone alert", any("locked" in a["title"] and a["priority"] == 1
                                                      for a in db.query("SELECT title, priority FROM alerts WHERE kind='auth'")))
    check("existing session unaffected by the lockout", remote.get("/api/status", headers=REMOTE).status_code == 200)
    auth.locked_until = 0

    # ---------------- expiry / logout ------------------------------------------------
    db.execute("UPDATE sessions SET expires=?", (time.time() - 1,))
    check("expired session rejected", remote.get("/api/status", headers=REMOTE).status_code == 401)
    remote.post("/api/auth/login", headers=REMOTE, json={"password": PW})
    tokr = remote.get("/api/session", headers=REMOTE).json()["token"]
    remote.post("/api/auth/logout", headers={**REMOTE, "X-Desk-Token": tokr}, json={})
    check("logout ends the session", remote.get("/api/status", headers=REMOTE).status_code == 401)
    a, b = TestClient(app, base_url="https://testserver"), TestClient(app, base_url="https://testserver")
    a.post("/api/auth/login", headers=REMOTE, json={"password": PW}); b.post("/api/auth/login", headers=REMOTE, json={"password": PW})
    local.post("/api/auth/logout", headers=LT, json={"everywhere": True})
    check("log out everywhere", a.get("/api/status", headers=REMOTE).status_code == 401 and b.get("/api/status", headers=REMOTE).status_code == 401)

    # ---------------- allowed Tailscale accounts ----------------------------------------
    remote.post("/api/auth/login", headers=REMOTE, json={"password": PW})
    tokr = remote.get("/api/session", headers=REMOTE).json()["token"]
    r = remote.post("/api/config", headers={**REMOTE, "X-Desk-Token": tokr}, json={"auth": {"allowed_tailscale_logins": [OTHER]}})
    check("remote can't save a list that locks itself out", r.status_code == 400 and cfg.data["auth"]["allowed_tailscale_logins"] == [], r.text)
    r = remote.post("/api/config", headers={**REMOTE, "X-Desk-Token": tokr}, json={"auth": {"allowed_tailscale_logins": [ME]}})
    check("allow-list saved", cfg.data["auth"]["allowed_tailscale_logins"] == [ME], r.text)
    stranger = {**REMOTE, "Tailscale-User-Login": OTHER}
    check("other Tailscale account blocked before the login page", remote.get("/login", headers=stranger).status_code == 403
          and remote.post("/api/auth/login", headers=stranger, json={"password": PW}).status_code == 403)
    check("allowed account still works", remote.get("/api/status", headers=REMOTE).status_code == 200)
    check("allow-list doesn't affect the server computer", local.get("/api/status").status_code == 200)

    # ---------------- password change ------------------------------------------------------
    r = remote.post("/api/auth/password", headers={**REMOTE, "X-Desk-Token": tokr}, json={"new": "another long password", "current": "bad"})
    check("remote change needs the current password", r.status_code == 400 and "Current" in r.json()["error"])
    r = remote.post("/api/auth/password", headers={**REMOTE, "X-Desk-Token": tokr}, json={"new": "another long password", "current": PW})
    check("remote change with current password works and logs everyone out", r.status_code == 200
          and remote.get("/api/status", headers=REMOTE).status_code == 401)
    r = remote.post("/api/auth/login", headers=REMOTE, json={"password": PW})
    check("old password no longer works", r.status_code == 401)
    check("new password works", remote.post("/api/auth/login", headers=REMOTE, json={"password": "another long password"}).status_code == 200)

    # ---------------- remote_url: server computer only, *.ts.net only ---------------------
    local.post("/api/config", headers=LT, json={"remote_url": "https://evil.example.com"})
    check("remote_url must be a ts.net address", cfg.data["remote_url"] == "")
    local.post("/api/config", headers=LT, json={"remote_url": "https://a-nexis.tail1234.ts.net/"})
    check("remote_url saved from the server computer", cfg.data["remote_url"] == "https://a-nexis.tail1234.ts.net")
    tokr = remote.get("/api/session", headers=REMOTE).json()["token"]
    remote.post("/api/config", headers={**REMOTE, "X-Desk-Token": tokr}, json={"remote_url": "https://phish.tail9.ts.net"})
    check("remote can't change remote_url", cfg.data["remote_url"] == "https://a-nexis.tail1234.ts.net")

    # ---------------- trading safety unchanged ----------------------------------------------
    check("no orders placed by any of this", not db.query("SELECT 1 FROM orders_log LIMIT 1"))
    check("env/auto/quoter untouched", cfg.env == "demo" and not cfg.auto["enabled"] and not cfg.quoter["enabled"])


if __name__ == "__main__":
    main()
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)
