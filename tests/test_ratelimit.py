"""Kalshi request budget: public readers share one budget per host; 429s wait as Kalshi asks and are counted."""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import kalshi_client as KC  # noqa: E402

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


class Resp:
    def __init__(self, code, headers=None, body=b'{"ok": true}'):
        self.status_code, self.headers, self.content = code, headers or {}, body
        self.text = body.decode()

    def json(self):
        import json
        return json.loads(self.content)


class Ses:
    def __init__(self, codes):
        self.codes = list(codes)
        self.n = 0

    def request(self, method, url, **kw):
        self.n += 1
        c = self.codes.pop(0)
        return Resp(c, {"Retry-After": "1"} if c == 429 else {})


def main():
    a = KC.KalshiClient("https://api.example.test/trade-api/v2")
    b = KC.KalshiClient("https://api.example.test/trade-api/v2")
    c = KC.KalshiClient("https://other.example.test/trade-api/v2")
    check("public readers of the same host share one request budget", a.read_bucket is b.read_bucket and a.read_bucket is not c.read_bucket)
    check("the shared budget is 2.5 reads a second with no burst", a.read_bucket.rate == 25 and a.read_bucket.capacity == 25)
    sleeps = []
    KC.time.sleep = lambda s: sleeps.append(s)
    a.read_bucket = KC.TokenBucket(rate=1e9, capacity=1e9)       # isolate the 429 waits from budget waits
    a.session = Ses([429, 429, 200])
    r = a.request("GET", "/markets")
    check("429: waits as long as Kalshi asks (Retry-After), then succeeds", r == {"ok": True} and a.session.n == 3
          and len(sleeps) >= 2 and all(1.0 <= s <= 1.26 for s in sleeps[-2:]), [round(x, 2) for x in sleeps[:12]])
    check("429s are counted for the dashboard", KC.rate_limit_stats().get("api.example.test", 0) >= 2)
    det = KC.rate_limit_detail().get("api.example.test", {})
    check("...and which calls were refused is kept (who, method, first path part)", det and all(k.split()[0] in ("public", "account") and k.split()[1] in ("GET", "POST", "DELETE", "PUT") and k.split()[2].startswith("/") for k in det), det)
    sleeps.clear()

    class NoRA(Ses):
        def request(self, method, url, **kw):
            self.n += 1
            self.codes.pop(0)
            return Resp(429, {})
    a.session = NoRA([429] * 7)
    try:
        a.request("GET", "/markets")
        ok = False
    except KC.KalshiError as e:
        ok = e.status == 429 if hasattr(e, "status") else True
    waits = [round(s) for s in sleeps]
    check("without Retry-After: backs off 0.5, 1, 2, 4, 8, 16 s (about 31 s in all) before giving up", ok and a.session.n == 7 and len(sleeps) == 6
          and sum(sleeps) >= 31 and sleeps[-1] >= 16, [round(x, 2) for x in sleeps[:12]])
    sleeps.clear()
    acct = KC.KalshiClient("https://api.example.test/trade-api/v2")
    acct.key_id, acct.private_key = "k", object()          # looks authenticated; signing is stubbed below
    acct._headers = lambda m, p: {}
    check("the account client stays under Kalshi's basic limits (20 reads/s, 10 writes/s) with no burst above one second's worth",
          (lambda k: k.read_bucket.rate == 120 and k.read_bucket.capacity <= k.read_bucket.rate and k.read_bucket.rate / 10 < 20
           and k.write_bucket.rate == 80 and k.write_bucket.capacity <= k.write_bucket.rate and k.write_bucket.rate / 10 < 10
           and k.read_bucket is not KC.shared_read_bucket(k.host))(KC.KalshiClient("https://api.example.test/trade-api/v2", "key-id", None)))
    acct.read_bucket = KC.TokenBucket(rate=1e9, capacity=1e9)       # an account client has its own budget (made so at creation)
    acct.session = NoRA([429] * 7)
    try:
        acct.request("GET", "/portfolio/balance")
    except KC.KalshiError:
        pass
    check("the account client (screens, quoter) gives up quickly on 429s: at most 3 short waits of up to 2 s",
          len(sleeps) <= 3 and all(s <= 2.26 for s in sleeps) and sum(sleeps) < 6, [round(x, 2) for x in sleeps[:12]])

    # ---- signed market-data reads (GET only) ----
    import tempfile
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    kp = Path(tempfile.mkdtemp()) / "test-only.pem"                 # a throwaway key made for this test
    kp.write_bytes(Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))

    class Rec:
        def __init__(self, codes):
            self.codes, self.seen = list(codes), []

        def request(self, method, url, headers=None, **kw):
            self.seen.append((method, url, dict(headers or {})))
            c = self.codes.pop(0)
            return Resp(c, {"Retry-After": "1"} if c == 429 else {})
    H = "https://signed.example.test/trade-api/v2"
    p1, p2 = KC.KalshiClient(H), KC.KalshiClient("https://unsigned.example.test/trade-api/v2")
    p1.session, p2.session = Rec([200, 200]), Rec([200])
    p1.request("GET", "/markets")
    check("without a signer a keyless read carries no key", "KALSHI-ACCESS-KEY" not in p1.session.seen[0][2])
    check("a signer whose key cannot be loaded changes nothing", KC.set_read_signer(H, "kid", str(kp) + ".missing") is False
          and "signed.example.test" not in KC.read_signer_status()["signed_hosts"])
    check("signer set for one host", KC.set_read_signer(H, "kid-1", str(kp)) is True and KC.read_signer_status()["signed_hosts"] == ["signed.example.test"])
    p1.request("GET", "/markets", {"limit": 5})
    p2.request("GET", "/markets")
    h1 = p1.session.seen[1][2]
    check("keyless reads of that host are now signed with the account key; other hosts are not",
          h1.get("KALSHI-ACCESS-KEY") == "kid-1" and h1.get("KALSHI-ACCESS-SIGNATURE") and "KALSHI-ACCESS-KEY" not in p2.session.seen[0][2], h1)
    check("signed reads draw on their own budget of 6 a second, well under Kalshi's 20, leaving room for the account client (12)",
          KC.SIGNED_READ == (60, 60) and (KC.SIGNED_READ[0] + KC.ACCOUNT_READ[0]) / 10 <= 18)
    blocked = 0
    for call in (lambda: p1.create_order(ticker="X", side="yes", action="buy", count=1, yes_price_dollars="0.50") if hasattr(p1, "create_order") else p1.request("POST", "/portfolio/events/orders", body={}, auth=True),
                 lambda: p1.request("POST", "/portfolio/events/orders", body={}, auth=True),
                 lambda: p1.request("DELETE", "/portfolio/events/orders/abc", auth=True),
                 lambda: p1.balance()):
        try:
            call()
        except KC.KalshiError as e:
            blocked += e.status == 401
        except TypeError:
            blocked += 1
    check("the signer never makes a keyless client able to trade or read the account: orders, cancels and balance are refused before sending",
          blocked == 4 and len(p1.session.seen) == 2 and not p1.authenticated, (blocked, len(p1.session.seen)))
    p1.session = Rec([200, 200, 200, 200])
    for path in ("/portfolio/fills", "/account/limits", "/markets/trades", "/events"):
        p1.request("GET", path)
    sg = ["KALSHI-ACCESS-KEY" in x[2] for x in p1.session.seen]
    check("only market-data paths are signed: a keyless client's GET to /portfolio or /account goes out without the key (and Kalshi refuses it)",
          sg == [False, False, True, True], sg)
    p1.session = Rec([200])
    p1.request("POST", "/anything", body={})
    check("a non-GET call is never signed with the read signer", "KALSHI-ACCESS-KEY" not in p1.session.seen[0][2])
    p1.session = Rec([401, 200, 200])
    p1.request("GET", "/markets")
    p1.request("GET", "/markets")
    check("if Kalshi refuses the key on a read, signing switches off for that host and the read goes through unsigned, as before",
          "KALSHI-ACCESS-KEY" in p1.session.seen[0][2] and "KALSHI-ACCESS-KEY" not in p1.session.seen[1][2] and "KALSHI-ACCESS-KEY" not in p1.session.seen[2][2]
          and "signed.example.test" in KC.read_signer_status()["off"], KC.read_signer_status())
    KC.set_read_signer(H, "kid-1", str(kp))
    p1.session = Rec([429, 200])
    p1.request("GET", "/events")
    check("a refused signed read is counted as 'signed', apart from public and account",
          KC.rate_limit_detail().get("signed.example.test", {}).get("signed GET /events") == 1, KC.rate_limit_detail())
    KC.clear_read_signer(H)
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
