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
        "quote_ttl_sec": 600,           # each quote expires on its own after this (re-placed 60s before; keeps queue priority)
        "max_volume_24h": 0,            # skip markets that traded more than this in 24h (0 = no limit)
        "select_by": "trading",         # "trading" = pick by estimated trading edge, rewards as a bonus; "rewards" = by reward per $
        "min_spread": 0.02,             # trading-first: skip markets with a tighter spread than this
        "market_loss_pct": 10,          # stop opening in a market once today's quoter loss there reaches this % of its capital (0 = off)
        "universe": "all",              # trading-first candidates: "all" open markets, or "rewards" = only markets with a reward program
        "min_volume_24h": 50,           # trading-first, markets without a reward program: need at least this 24h volume
        "exit_step_in": True,           # at the inventory cap, the reducing quote steps 1c inside the spread (if >= 2c wide)
        "offset_ticks": 0,              # opening quotes rest this many cents BEHIND the best bid (0 = join the best bid)
        "max_move": 0,                  # trading-first: skip markets whose price moved more than this since yesterday's close (0 = off; 0.03 = 3c)
        "pull_after_fill_min": 0,       # after a fill that opens/adds to a position, stop quoting that side of that market this long (0 = off)
    },
    # --- limits that scale with the account (see app/scaling.py); tiers default to scaling.DEFAULT_TIERS ---
    "scaling": {
        "enabled": False,
        "caps": {},                     # optional $ ceilings: order, daily_buys, market, total, daily_loss, strat_day, strat_7d, drawdown, quoter
        "max_markets_cap": 5,           # most markets the quoter may use when scaled
        "base": None, "base_day": None, "base_env": None, "tier": None, "limits": None,
        # DEMO only: size the demo account as if it held this many dollars (0 = off), so demo behaves at production size.
        # Set through the app (Settings > Limits & safety), which records the starting point below.
        "demo_base": 0, "demo_since": 0, "demo_equity0": None, "demo_ignore": {}, "demo_spent": {},
    },
    # --- exit rules: per-position take-profit / stop-loss (closing orders only) ---
    "exits": {"enabled": True, "check_sec": 30, "confirm_checks": 2},
    # --- nightly backup + pruning ---
    "maintenance": {"enabled": True, "time": "03:30", "keep_days": 7, "prune_days": 30, "warehouse_max_gb": 4},
    # --- circuit breakers (Phase 3) - every limit 0 = off until you set it ----------
    "breakers": {
        "enabled": True,                # compute P&L / drawdown every check_min (acts only on limits > 0)
        "check_min": 5,
        "strategy_daily_loss": 0.0,     # $ a strategy may lose today (its fills, marked to market) before it is turned off
        "strategy_7d_loss": 0.0,        # $ over the last 7 days
        "account_drawdown": 0.0,        # $ below the account's equity peak -> kill switch
        "drawdown_days": 30,            # peak lookback
        "tripped": {},                  # strategy -> {ts, reason}; set by the app, cleared by you (Settings)
        "peak_reset_ts": 0,
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
    # --- remote access (Tailscale serve + password) ---------------------------
    "auth": {
        "password_hash": "", "salt": "", "iterations": 600000,
        "session_days": 30,
        "allowed_tailscale_logins": [],   # e.g. ["you@gmail.com"]; empty = any device in your tailnet (still needs the password)
    },
    "remote_url": "",                     # https://<machine>.<tailnet>.ts.net (set by the setup script)
    # --- research data warehouse (production PUBLIC market data, no key) ------
    "warehouse": {
        "enabled": True,
        "categories": ["Climate and Weather"],
        "tags": ["Daily temperature"],  # only series in those categories with one of these tags ([] = all)
        "series": [],                   # extra series tickers to collect (no tag filter)
        "lookback_days": 120,
        "req_per_sec": 3,
        "candle_period": 60,            # minutes (1, 60 or 1440)
        "sync_interval_min": 60,
        "record_ticks": True,           # top-of-book changes from the live feed
        "max_hist_pages": 20,
        "base_url_override": "",        # testing only
        # Broad sample for the edge studies: besides the full scope above, a slice of EVERY other category -
        # the busiest series of each, and the most recently settled markets of each series.
        "sample": {"enabled": False, "series_per_category": 12, "markets_per_series": 60, "days": 45,
                   "max_trades_per_market": 5000, "skip_categories": []},
    },
    # --- weather data for 2.1 (NWS forecast guidance + observations + climate reports, public) ---
    "weather": {
        "enabled": True,                # collect data only; no trading
        "sync_interval_min": 20,
        "lookback_days": 130,
        "request_gap_sec": 1.0,         # IEM rate-limits; it also backs off on HTTP 429
        "backoff_sec": [20, 60, 180],
    },
    # --- sports odds research (2.2): OddsPapi free tier (250 requests/month) ------
    "sports": {
        "oddspapi_key": "",             # entered in Settings, never shown again
        "monthly_cap": 240,             # hard stop below the free plan's 250 requests/month
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
                try:
                    raw = json.loads(self.path.read_text("utf-8"))
                    if not isinstance(raw, dict):
                        raise ValueError("config is not an object")
                    self.recovered = None
                except (ValueError, OSError) as e:
                    # damaged file (e.g. power loss mid-write on an older version): use the last good copy
                    bak = self.path.with_suffix(".json.bak")
                    raw = json.loads(bak.read_text("utf-8")) if bak.exists() else {}
                    self.recovered = f"config.json unreadable ({e}); " + ("restored from config.json.bak" if raw else "defaults loaded")
                    try:
                        self.path.replace(self.path.with_suffix(".json.damaged"))
                    except OSError:
                        pass
                    self.data = _merge(DEFAULTS, raw)
                    self._write()
                    return
                self.data = _merge(DEFAULTS, raw)
            else:
                self.recovered = None
                self.data = json.loads(json.dumps(DEFAULTS))
                self._write()

    def _write(self):
        """Atomic: write a temp file, flush to disk, then swap it in. The previous good file is kept as .bak."""
        txt = json.dumps(self.data, indent=2)
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(txt)
            f.flush()
            os.fsync(f.fileno())
        if self.path.exists():
            try:
                json.loads(self.path.read_text("utf-8"))
                bak = self.path.with_suffix(".json.bak")
                bak.write_bytes(self.path.read_bytes())
            except (ValueError, OSError):
                pass
        os.replace(tmp, self.path)

    def save(self):
        with self._lock:
            self._write()

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
        au = d.get("auth", {})
        for k in ("password_hash", "salt", "iterations"):
            au.pop(k, None)
        au["password_set"] = bool(self.data.get("auth", {}).get("password_hash"))
        a = d.get("alerts", {})
        for k in ("pushover_user", "pushover_token"):
            a[k] = (a[k][:4] + "…") if a.get(k) else ""
        sp = d.get("sports", {})
        if sp.get("oddspapi_key"):
            sp["oddspapi_key"] = sp["oddspapi_key"][:4] + "…"
        if a.get("healthcheck_url"):
            a["healthcheck_url"] = a["healthcheck_url"][:30] + "…"
        return d
