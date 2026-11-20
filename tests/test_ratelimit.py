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
    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
