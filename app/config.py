"""Configuration: loaded from config.json next to the app (created on first run)."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("KALSHI_DESK_CONFIG", ROOT / "config.json"))

BASE_URLS = {
    "demo": "https://external-api.demo.kalshi.co/trade-api/v2",
    "prod": "https://external-api.kalshi.com/trade-api/v2",
}

DEFAULTS = {
    # --- connection -------------------------------------------------------
    "env": "demo",                 # "demo" or "prod"
    "base_url_override": "",       # testing only; leave blank
    "demo_key_id": "",
    "demo_private_key_path": "",
    "prod_key_id": "",
    "prod_private_key_path": "",
    "port": 8765,
    "ws_enabled": True,            # real-time WebSocket feed (falls back to REST polling)
    "ws_url_override": "",         # testing only; leave blank
    # --- risk limits (dollars) --------------------------------------------
    "risk": {
        "max_order_cost": 25.0,         # max $ at risk on a single order
        "max_daily_spend": 100.0,       # max $ of new buys per calendar day (local)
        "max_market_exposure": 50.0,    # max $ at risk in any one market
        "max_total_exposure": 250.0,    # max $ at risk across all open positions
        "max_contracts_per_order": 500,
        "max_daily_loss": 25.0,         # equity down this much today -> kill switch trips
        "min_price": 0.10,              # refuse to buy below this (longshots lose >60% on avg)
        "max_price": 0.98,              # refuse to buy above this (no-upside guard)
        "require_confirm_prod": True,   # UI asks twice before any prod order
        "kill_switch": False,           # True = no new orders at all
        "blocked_categories": [],
    },
    # --- automation -------------------------------------------------------
    "auto": {
        "enabled": False,               # master switch for auto-execution
        "strategies": [],               # strategy ids allowed to auto-trade
        "max_order_cost": 10.0,         # tighter cap for auto orders
        "max_daily_spend": 30.0,
    },
    # --- liquidity-reward quoter --------------------------------------------
    "quoter": {
        "enabled": False,
        "max_markets": 5,               # markets quoted at once
        "size": 10,                     # contracts per quote
        "min_daily_reward": 5.0,        # $/day pool to bother with a market
        "max_inventory": 30,            # max net contracts held per market before one side stops
        "max_capital": 100.0,           # $ cap: resting quotes + quoter inventory
        "pull_hours": 6,                # stop quoting this many hours before expected settlement
        "refresh_sec": 15,
        "jump_cents": 4,                # mid moves this much in one cycle -> pull quotes + cool down
        "cooldown_min": 10,
        "min_mid": 0.15, "max_mid": 0.85,
        "max_spread": 0.06,
    },
    # --- phone alerts (Pushover) --------------------------------------------
    "alerts": {
        "enabled": True,
        "pushover_user": "",            # Pushover user key (30 chars) - entered in Settings, never shown again
        "pushover_token": "",           # Pushover application API token (30 chars)
        "device": "",                   # optional: only this Pushover device
        "fills": True,
        "kill": True,                   # kill switch + leg risk (emergency priority)
        "errors": True,
        "lifecycle": True,              # app started / restarted / down (watchdog)
        "daily_summary": True,
        "summary_time": "23:50",        # local time
        "fill_batch_sec": 10,
        "error_throttle_min": 30,
        "network_errors_to_alert": 3,   # transient network/5xx errors alert only at this many in 15 min
        "feed_down_min": 5,
        "emergency_retry": 60,          # seconds between repeats until acknowledged (min 30)
        "emergency_expire": 3600,       # stop repeating after this many seconds (max 10800)
        "app_down_min": 2,              # watchdog: alert after the app is unreachable this long
        "healthcheck_url": "",          # optional dead-man ping (e.g. healthchecks.io) every minute
        "api_url_override": "",         # testing only
    },
    # --- scanner ----------------------------------------------------------
    "scanner": {
        "interval_sec": 120,
        "min_edge": 0.02,               # $ per contract after fees
        "min_volume_24h": 50,
        "max_markets_scanned": 4000,
        "categories": [],               # empty = all
        "strategies": ["basket_no", "basket_yes", "fair_value", "wide_spread", "movers"],
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            out[k] = _merge(base[k], v)
        else:
            out[k] = v
    return out


class Config:
    _lock = threading.Lock()

    def __init__(self, path: Path = CONFIG_PATH):
        self.path = Path(path)
        self.data = DEFAULTS
        self.load()

    def load(self):
        with self._lock:
            if self.path.exists():
                self.data = _merge(DEFAULTS, json.loads(self.path.read_text("utf-8")))
            else:
                self.data = json.loads(json.dumps(DEFAULTS))
                self.path.write_text(json.dumps(self.data, indent=2), "utf-8")

    def save(self):
        with self._lock:
            self.path.write_text(json.dumps(self.data, indent=2), "utf-8")

    def update(self, patch: dict):
        self.data = _merge(self.data, patch)
        if self.data["env"] not in BASE_URLS:
            raise ValueError("env must be 'demo' or 'prod'")
        self.save()

    # convenience ----------------------------------------------------------
    @property
    def env(self) -> str:
        return self.data["env"]

    @property
    def base_url(self) -> str:
        return self.data.get("base_url_override") or BASE_URLS[self.env]

    @property
    def key_id(self) -> str:
        return self.data[f"{self.env}_key_id"]

    @property
    def key_path(self) -> str:
        return self.data[f"{self.env}_private_key_path"]

    @property
    def risk(self) -> dict:
        return self.data["risk"]

    @property
    def auto(self) -> dict:
        return self.data["auto"]

    @property
    def scanner(self) -> dict:
        return self.data["scanner"]

    @property
    def quoter(self) -> dict:
        return self.data["quoter"]

    def public_view(self) -> dict:
        """Config safe to send to the browser (no key paths)."""
        d = json.loads(json.dumps(self.data))
        for e in ("demo", "prod"):
            d[f"{e}_private_key_path"] = bool(d[f"{e}_private_key_path"])
            kid = d[f"{e}_key_id"]
            d[f"{e}_key_id"] = (kid[:8] + "…") if kid else ""
        a = d.get("alerts", {})
        for k in ("pushover_user", "pushover_token"):
            a[k] = (a[k][:4] + "…") if a.get(k) else ""
        if a.get("healthcheck_url"):
            a["healthcheck_url"] = a["healthcheck_url"][:30] + "…"
        return d
