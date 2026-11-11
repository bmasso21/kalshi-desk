"""Thin, dependency-light client for the Kalshi Trade API v2.

Built against Kalshi's OpenAPI spec v3.35.0 (Nov 2026):
  * auth: KALSHI-ACCESS-KEY / -SIGNATURE / -TIMESTAMP headers; signature over
    timestamp_ms + METHOD + path (no query string). Ed25519 or RSA-PSS/SHA256.
  * orders: V2 endpoint POST /portfolio/events/orders, side = bid|ask on the
    YES book, fixed-point dollar price strings and count strings.
  * rate limits (Basic tier): read 200 tok/s (burst 600), write 100 tok/s,
    10 tokens per request by default; 429 -> exponential backoff.
"""
from __future__ import annotations

import base64
import threading
import time
import uuid
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class KalshiError(Exception):
    def __init__(self, status: int, payload: Any):
        self.status = status
        self.payload = payload
        msg = payload.get("message") or payload.get("error") if isinstance(payload, dict) else payload
        super().__init__(f"Kalshi API {status}: {msg}")


# ---------------------------------------------------------------- signing --
def load_private_key(path: str):
    data = Path(path).expanduser().read_bytes()
    return serialization.load_pem_private_key(data, password=None)


def sign(private_key, text: str) -> str:
    msg = text.encode("utf-8")
    if isinstance(private_key, Ed25519PrivateKey):
        sig = private_key.sign(msg)
    else:
        sig = private_key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    return base64.b64encode(sig).decode("ascii")


# ----------------------------------------------------------- rate limiter --
class TokenBucket:
    def __init__(self, rate: float, capacity: float):
        self.rate, self.capacity = rate, capacity
        self.tokens = capacity
        self.ts = time.monotonic()
        self.lock = threading.Lock()

    def take(self, n: float = 10):
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.ts) * self.rate)
                self.ts = now
                if self.tokens >= n:
                    self.tokens -= n
                    return
                wait = (n - self.tokens) / self.rate
            time.sleep(wait)


# ----------------------------------------------------------------- prices --
def d(x) -> Decimal:
    return Decimal(str(x)) if x is not None and x != "" else Decimal(0)


def price_str(p) -> str:
    """Kalshi request prices: fixed-point dollars, 2-4 decimals."""
    return str(d(p).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def count_str(c) -> str:
    return str(d(c).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ----------------------------------------------------------------- client --
class KalshiClient:
    def __init__(self, base_url: str, key_id: str = "", private_key_path: str = "", timeout: float = 15):
        self.base_url = base_url.rstrip("/")
        self.path_prefix = urlparse(self.base_url).path  # e.g. /trade-api/v2
        self.key_id = key_id
        self.private_key = load_private_key(private_key_path) if private_key_path else None
        self.timeout = timeout
        self.session = requests.Session()
        self.read_bucket = TokenBucket(rate=200, capacity=600)
        self.write_bucket = TokenBucket(rate=100, capacity=100)
        self.net_retries = 0

    @property
    def authenticated(self) -> bool:
        return bool(self.key_id and self.private_key)

    # -- core ---------------------------------------------------------------
    def _headers(self, method: str, path: str) -> dict:
        h = {"Accept": "application/json"}
        if self.authenticated:
            ts = str(int(time.time() * 1000))
            signed_path = self.path_prefix + path.split("?")[0]
            h.update({
                "KALSHI-ACCESS-KEY": self.key_id,
                "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": sign(self.private_key, ts + method.upper() + signed_path),
            })
        return h

    def request(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
                auth: bool = False, retries: int = 5) -> dict:
        if auth and not self.authenticated:
            raise KalshiError(401, {"message": "No API key configured for this environment"})
        is_write = method.upper() in ("POST", "PUT", "DELETE")
        params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        delay = 0.25
        # Reads and cancels are safe to repeat after a dropped connection; new orders are NOT
        # (the order may have reached Kalshi), so POST/PUT are never retried on network errors.
        retry_net = method.upper() in ("GET", "DELETE")
        for attempt in range(retries):
            (self.write_bucket if is_write else self.read_bucket).take(10)
            try:
                resp = self.session.request(method, self.base_url + path, params=params, json=body,
                                            headers=self._headers(method, path), timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                if retry_net and attempt < retries - 1:
                    self.net_retries += 1
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise
            if resp.status_code == 429 and attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            if resp.status_code >= 500 and not is_write and attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            try:
                payload = resp.json() if resp.content else {}
            except ValueError:
                payload = {"message": resp.text[:300]}
            if resp.status_code >= 400:
                raise KalshiError(resp.status_code, payload)
            return payload
        raise KalshiError(429, {"message": "rate limited after retries"})

    def paginate(self, path: str, key: str, params: dict | None = None, auth: bool = False,
                 max_items: int | None = None) -> Iterator[dict]:
        params = dict(params or {})
        n = 0
        while True:
            page = self.request("GET", path, params=params, auth=auth)
            for item in page.get(key) or []:
                yield item
                n += 1
                if max_items and n >= max_items:
                    return
            cur = page.get("cursor")
            if not cur:
                return
            params["cursor"] = cur

    # -- exchange -----------------------------------------------------------
    def exchange_status(self):
        return self.request("GET", "/exchange/status")

    # -- market data (public) ----------------------------------------------
    def series_list(self, category: str | None = None):
        return self.request("GET", "/series", {"category": category, "include_volume": "true"}).get("series") or []

    def series(self, ticker: str):
        return self.request("GET", f"/series/{ticker}").get("series")

    def tags_by_category(self):
        return self.request("GET", "/search/tags_by_categories")

    def events(self, status="open", series_ticker=None, nested=True, max_items=None):
        return list(self.paginate("/events", "events", {
            "status": status, "series_ticker": series_ticker,
            "with_nested_markets": "true" if nested else None, "limit": 200,
        }, max_items=max_items))

    def event(self, event_ticker: str):
        return self.request("GET", f"/events/{event_ticker}", {"with_nested_markets": "true"})

    def markets(self, status="open", event_ticker=None, series_ticker=None, tickers=None,
                exclude_combos=True, max_items=None):
        return list(self.paginate("/markets", "markets", {
            "status": status, "event_ticker": event_ticker, "series_ticker": series_ticker,
            "tickers": ",".join(tickers) if tickers else None,
            "mve_filter": "exclude" if exclude_combos else None, "limit": 1000,
        }, max_items=max_items))

    def market(self, ticker: str):
        return self.request("GET", f"/markets/{ticker}").get("market")

    def orderbook(self, ticker: str, depth: int = 10):
        return self.request("GET", f"/markets/{ticker}/orderbook", {"depth": depth}).get("orderbook_fp") or {}

    def orderbooks(self, tickers: list[str], batch: int = 20) -> dict:
        """Batch orderbooks (public). Returns {ticker: orderbook_fp}."""
        out = {}
        for i in range(0, len(tickers), batch):
            page = self.request("GET", "/markets/orderbooks", {"tickers": tickers[i:i + batch]})
            for ob in page.get("orderbooks") or []:
                out[ob.get("ticker")] = ob.get("orderbook_fp") or {}
        return out

    def candlesticks(self, series_ticker: str, ticker: str, start_ts: int, end_ts: int, period: int = 60):
        return self.request("GET", f"/series/{series_ticker}/markets/{ticker}/candlesticks",
                            {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period})

    # -- portfolio (auth) ---------------------------------------------------
    def balance(self):
        return self.request("GET", "/portfolio/balance", auth=True)

    def positions(self, settlement_status: str | None = None):
        out = {"market_positions": [], "event_positions": []}
        params = {"limit": 1000, "count_filter": "position", "settlement_status": settlement_status}
        while True:
            page = self.request("GET", "/portfolio/positions", params, auth=True)
            out["market_positions"] += page.get("market_positions") or []
            out["event_positions"] += page.get("event_positions") or []
            if not page.get("cursor"):
                return out
            params["cursor"] = page["cursor"]

    def orders(self, status: str | None = "resting", ticker: str | None = None, max_items=500,
               min_ts: int | None = None):
        return list(self.paginate("/portfolio/orders", "orders",
                                  {"status": status, "ticker": ticker, "limit": 200, "min_ts": min_ts},
                                  auth=True, max_items=max_items))

    def credit_history(self, min_ts: int | None = None, max_items=1000):
        return list(self.paginate("/portfolio/credit_history", "credits", {"min_ts": min_ts, "limit": 200},
                                  auth=True, max_items=max_items))

    def incentives(self, status="active", type_="liquidity", max_items=5000):
        """Incentive programs. period_reward is in centi-cents ($1 = 10,000)."""
        out, params = [], {"status": status, "type": type_, "limit": 1000}
        while True:
            page = self.request("GET", "/incentive_programs", params)
            out += page.get("incentive_programs") or []
            cur = page.get("next_cursor")
            if not cur or len(out) >= max_items:
                return out
            params["cursor"] = cur

    def fills(self, min_ts: int | None = None, max_items=1000):
        return list(self.paginate("/portfolio/fills", "fills", {"min_ts": min_ts, "limit": 200},
                                  auth=True, max_items=max_items))

    def settlements(self, min_ts: int | None = None, max_items=1000):
        return list(self.paginate("/portfolio/settlements", "settlements", {"min_ts": min_ts, "limit": 200},
                                  auth=True, max_items=max_items))

    # -- orders (auth, V2) --------------------------------------------------
    def create_order(self, ticker: str, book_side: str, price, count, time_in_force="good_till_canceled",
                     post_only=False, reduce_only=False, expiration_time: int | None = None,
                     client_order_id: str | None = None):
        """book_side: 'bid' = buy YES at price; 'ask' = sell YES at price (= buy NO at 1-price)."""
        if book_side not in ("bid", "ask"):
            raise ValueError("book_side must be 'bid' or 'ask'")
        body = {
            "ticker": ticker,
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "side": book_side,
            "count": count_str(count),
            "price": price_str(price),
            "time_in_force": time_in_force,
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": bool(post_only),
            "reduce_only": bool(reduce_only),
            "cancel_order_on_pause": True,
        }
        if expiration_time:
            body["expiration_time"] = int(expiration_time)
        return self.request("POST", "/portfolio/events/orders", body=body, auth=True)

    def cancel_order(self, order_id: str, market_ticker: str | None = None):
        return self.request("DELETE", f"/portfolio/events/orders/{order_id}",
                            {"market_ticker": market_ticker}, auth=True)

    def amend_order(self, order_id: str, ticker: str, book_side: str, price, count):
        return self.request("POST", f"/portfolio/events/orders/{order_id}/amend", body={
            "ticker": ticker, "side": book_side, "price": price_str(price), "count": count_str(count),
        }, auth=True)


# ---------------------------------------------------------- book helpers --
def best_quotes(market: dict) -> dict:
    """Normalise top-of-book from a Market object (all dollars as Decimal)."""
    yb, ya = d(market.get("yes_bid_dollars")), d(market.get("yes_ask_dollars"))
    nb, na = d(market.get("no_bid_dollars")), d(market.get("no_ask_dollars"))
    return {"yes_bid": yb, "yes_ask": ya, "no_bid": nb, "no_ask": na,
            "last": d(market.get("last_price_dollars")),
            "prev": d(market.get("previous_price_dollars"))}


def book_from_orderbook(ob: dict) -> dict:
    """orderbook_fp has YES bids and NO bids only, each level [price_str, count_str].
    YES ask at p == NO bid at 1-p."""
    yes = sorted(((d(p), d(c)) for p, c in ob.get("yes_dollars") or []), key=lambda x: -x[0])
    no = sorted(((d(p), d(c)) for p, c in ob.get("no_dollars") or []), key=lambda x: -x[0])
    yes_asks = sorted(((Decimal(1) - p, c) for p, c in no), key=lambda x: x[0])
    no_asks = sorted(((Decimal(1) - p, c) for p, c in yes), key=lambda x: x[0])
    return {"yes_bids": yes, "yes_asks": yes_asks, "no_bids": no, "no_asks": no_asks}
