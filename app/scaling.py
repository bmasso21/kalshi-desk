"""Limits that scale with the account (v3.2).

When enabled, every dollar limit is a percentage of the account's value (cash + positions, Kalshi's numbers) taken
once a day (first check after local midnight) or when you press "Recalculate now". The base stays fixed during the
day, so a winning morning doesn't loosen the limits and a losing one doesn't shrink them mid-trade. A deposit therefore
takes effect the next morning (or on Recalculate).

Tiers: the percentages tighten as the account grows. To avoid a limit DROPPING when the account crosses into a
tighter tier, each limit is never less than what it was at the top of the previous tier.

    limit(equity) = max( pct_current_tier x equity , limit at the previous tier's upper bound )

Optional hard caps (`caps`) put a dollar ceiling on any limit.

What is scaled:
  risk      max_order_cost, max_daily_spend, max_market_exposure, max_total_exposure, max_daily_loss
  auto      max_order_cost = 40% of the order limit, max_daily_spend = 30% of the daily-buys limit (same ratio as before)
  breakers  strategy_daily_loss, strategy_7d_loss, account_drawdown
  quoter    max_capital; markets = min(your Markets setting cap, capital / $10, at least 1);
            contracts per quote = capital / markets / 2 (room for both quotes plus inventory); inventory cap = 2 x that

Deposits and withdrawals (Kalshi GET /portfolio/deposits, /portfolio/withdrawals; status "applied") are netted out of
the daily-loss and drawdown calculations so a withdrawal is not mistaken for a loss, nor a deposit for a profit.
"""
from __future__ import annotations

import math
import time

from . import risk

KEYS = ("order", "daily_buys", "market", "total", "daily_loss", "strat_day", "strat_7d", "drawdown", "quoter")
LABELS = {"order": "Per order", "daily_buys": "New buys per day", "market": "Per market", "total": "Total exposure",
          "daily_loss": "Daily loss (kill switch)", "strat_day": "Breaker: per strategy per day",
          "strat_7d": "Breaker: per strategy over 7 days", "drawdown": "Breaker: account drop below peak",
          "quoter": "Quoter capital"}
DEFAULT_TIERS = [
    {"upto": 250, "order": 20, "daily_buys": 50, "market": 30, "total": 80, "daily_loss": 20, "strat_day": 15,
     "strat_7d": 30, "drawdown": 30, "quoter": 80},
    {"upto": 1000, "order": 10, "daily_buys": 30, "market": 15, "total": 80, "daily_loss": 12, "strat_day": 8,
     "strat_7d": 16, "drawdown": 20, "quoter": 60},
    {"upto": None, "order": 5, "daily_buys": 20, "market": 10, "total": 70, "daily_loss": 10, "strat_day": 5,
     "strat_7d": 12, "drawdown": 15, "quoter": 50},
]


def _cents(x: float) -> float:
    return math.floor(max(0.0, x) * 100 + 1e-6) / 100


def compute(equity: float, tiers: list[dict] | None = None, caps: dict | None = None) -> dict:
    """Dollar limits for this equity. Returns {"tier": index, "limits": {key: $}}."""
    tiers = tiers or DEFAULT_TIERS
    equity = max(0.0, float(equity))
    out, floor = {}, {k: 0.0 for k in KEYS}
    idx = len(tiers) - 1
    for i, t in enumerate(tiers):
        up = t.get("upto")
        if up is None or equity < float(up):
            idx = i
            break
        for k in KEYS:                       # value at the top of a finished tier = floor for the next ones
            floor[k] = max(floor[k], float(t[k]) / 100 * float(up))
    t = tiers[idx]
    for k in KEYS:
        v = max(float(t[k]) / 100 * equity, floor[k])
        v = min(v, equity) if k != "daily_buys" else v
        cap = (caps or {}).get(k)
        if cap not in (None, "", 0):
            v = min(v, float(cap))
        out[k] = _cents(v)
    return {"tier": idx, "limits": out}


QUOTE_MAX_PRICE = 0.85      # the quoter's highest quote price (its max_mid default): a quote costs at most this per contract


def quoter_shape(capital: float, markets_cap: int, order_limit: float | None = None) -> dict:
    """Markets, contracts per quote and inventory cap for a quoter capital. With `order_limit`, the quote size is
    also capped so that one quote can never cost more than the per-order limit (small accounts: $2 -> 2 contracts)."""
    markets = max(1, min(int(markets_cap or 5), int(capital // 10)))
    size = max(1, int(capital / markets / 2))
    if order_limit:
        size = max(1, min(size, int(float(order_limit) / QUOTE_MAX_PRICE + 1e-9)))
    return {"max_markets": markets, "size": size, "max_inventory": 2 * size}


def to_patch(limits: dict, markets_cap: int) -> dict:
    q = quoter_shape(limits["quoter"], markets_cap, limits["order"])
    return {
        "risk": {"max_order_cost": limits["order"], "max_daily_spend": limits["daily_buys"],
                 "max_market_exposure": limits["market"], "max_total_exposure": limits["total"],
                 "max_daily_loss": limits["daily_loss"]},
        "auto": {"max_order_cost": _cents(0.4 * limits["order"]), "max_daily_spend": _cents(0.3 * limits["daily_buys"])},
        "breakers": {"strategy_daily_loss": limits["strat_day"], "strategy_7d_loss": limits["strat_7d"],
                     "account_drawdown": limits["drawdown"]},
        "quoter": {"max_capital": limits["quoter"], **q},
    }


SCALED_FIELDS = {"risk": ("max_order_cost", "max_daily_spend", "max_market_exposure", "max_total_exposure", "max_daily_loss"),
                 "auto": ("max_order_cost", "max_daily_spend"),
                 "breakers": ("strategy_daily_loss", "strategy_7d_loss", "account_drawdown"),
                 "quoter": ("max_capital", "max_markets", "size", "max_inventory")}


class Scaler:
    def __init__(self, engine):
        self.e = engine
        self.error = None

    @property
    def cfg(self) -> dict:
        return self.e.cfg.data.setdefault("scaling", {})

    def apply(self, force: bool = False, equity: float | None = None) -> dict | None:
        """Recompute and apply the limits if enabled and not yet done today (or force). Returns the result."""
        c = self.cfg
        if not c.get("enabled"):
            return None
        today = risk.today_key()
        env = self.e.cfg.env
        if not force and c.get("base_day") == today and c.get("base_env") == env:
            return None
        if equity is None:
            if not self.e.client or not self.e.client.authenticated:
                return None
            equity = self.sim_equity(float(self.e.equity()))
        r = compute(equity, c.get("tiers") or DEFAULT_TIERS, c.get("caps"))
        patch = to_patch(r["limits"], int(c.get("max_markets_cap") or 5))
        patch["scaling"] = {"base": round(equity, 2), "base_day": today, "base_env": env, "base_ts": time.time(),
                            "tier": r["tier"], "limits": r["limits"]}
        self.e.cfg.update(patch)
        self.e.db.audit("scaling", f"{env}: limits set from {'DEMO TEST BALANCE' if self.sim() else 'account value'} ${equity:.2f} (tier {r['tier'] + 1}): "
                                   + ", ".join(f"{k} ${v:.2f}" for k, v in r["limits"].items()))
        self.error = None
        return r

    # ---- demo test balance: make the DEMO account behave like an account of a chosen size -------------------
    def sim(self) -> dict | None:
        """The scaling settings when the demo test balance is active (demo environment, scaling on, demo_base > 0)."""
        c = self.cfg
        return c if (self.e.cfg.env == "demo" and c.get("enabled") and float(c.get("demo_base") or 0) > 0) else None

    def sim_equity(self, real: float) -> float:
        """Pretend equity = the test balance plus whatever the demo account has made or lost since it was switched on."""
        c = self.sim()
        if not c:
            return real
        e0 = c.get("demo_equity0")
        return max(0.0, float(c["demo_base"]) + real - float(e0 if e0 is not None else real))

    def start_sim(self, base: float) -> dict:
        """Start (or restart) the demo test balance: limits are sized from `base`; what the account already holds and
        has already spent or lost today is set aside, so the pretend account starts clean. Demo only."""
        if self.e.cfg.env != "demo":
            raise ValueError("the test balance only works in the DEMO environment")
        if not self.cfg.get("enabled"):
            raise ValueError("turn on 'Scale limits with the account' first")
        if not self.e.client or not self.e.client.authenticated:
            raise ValueError("no working API key for demo")
        base = float(base)
        if not 1 <= base <= 1_000_000:
            raise ValueError("test balance must be between $1 and $1,000,000")
        now, real = time.time(), float(self.e.equity())
        pos = self.e.positions(max_age=0) or {}
        ignore = {mp["ticker"]: round(abs(float(mp.get("market_exposure_dollars") or 0)), 4)
                  for mp in pos.get("market_positions", []) if float(mp.get("position_fp") or 0) != 0}
        sp = self.e.spent_today(max_age=0)
        self.e.cfg.data.setdefault("scaling", {})["demo_ignore"] = ignore           # replace, not merge
        self.e.cfg.update({"scaling": {"demo_base": base, "demo_since": now, "demo_equity0": round(real, 2),
                                       "demo_spent": {"day": risk.today_key(), "all": float(sp["all"]), "auto": float(sp["auto"])}}})
        # today's P&L and the drawdown peak start from now: earlier results belong to the real demo balance
        self.e.db.execute("INSERT OR REPLACE INTO day_equity VALUES(?,?,?,?)", ("demo", risk.today_key(), real, now))
        if getattr(self.e, "breakers", None):
            self.e.breakers.reset(None, peak=True)
        self.e.db.audit("scaling", f"demo test balance ${base:.2f} started (real demo equity ${real:.2f}; "
                                   f"{len(ignore)} existing position(s) and ${float(sp['all']):.2f} of today's buys set aside)")
        return self.apply(force=True)

    def stop_sim(self):
        self.e.cfg.data.setdefault("scaling", {})["demo_ignore"] = {}
        self.e.cfg.update({"scaling": {"demo_base": 0, "demo_since": 0, "demo_equity0": None, "demo_spent": {}}})
        self.e.db.audit("scaling", "demo test balance switched off; limits follow the real demo balance again")
        return self.apply(force=True)

    def risk_view(self, positions, spent):
        """Positions and today's spend as the pretend account sees them: exposure and buys from before the test
        balance started are left out. Only used for the pre-trade limits; everything shown to you stays real."""
        c = self.sim()
        if not c:
            return positions, spent
        ign = c.get("demo_ignore") or {}
        if positions and ign:
            mps = []
            for mp in positions.get("market_positions", []):
                cut = float(ign.get(mp.get("ticker")) or 0)
                if cut > 0:
                    mp = {**mp, "market_exposure_dollars": f"{max(0.0, abs(float(mp.get('market_exposure_dollars') or 0)) - cut):.4f}"}
                mps.append(mp)
            positions = {**positions, "market_positions": mps}
        off = c.get("demo_spent") or {}
        if spent and off.get("day") == risk.today_key():
            from decimal import Decimal
            spent = {**spent, "all": max(Decimal(0), spent["all"] - Decimal(str(off.get("all") or 0))),
                     "auto": max(Decimal(0), spent["auto"] - Decimal(str(off.get("auto") or 0)))}
        return positions, spent

    def snapshot(self) -> dict:
        c = self.cfg
        tiers = c.get("tiers") or DEFAULT_TIERS
        return {"demo": {"active": bool(self.sim()), "base": c.get("demo_base") or 0, "since": c.get("demo_since") or 0,
                         "equity0": c.get("demo_equity0"), "set_aside": c.get("demo_ignore") or {}, "env": self.e.cfg.env},
                "enabled": bool(c.get("enabled")), "base": c.get("base"), "base_day": c.get("base_day"),
                "base_env": c.get("base_env"), "tier": c.get("tier"), "limits": c.get("limits"), "tiers": tiers,
                "caps": c.get("caps") or {}, "labels": LABELS, "error": self.error,
                "examples": {str(x): compute(x, tiers, c.get("caps"))["limits"] for x in (10, 110, 500, 1000, 5000)}}


# ------------------------------------------------------------- transfers --
class Transfers:
    """Applied deposits and withdrawals from Kalshi, cached. net(since) = deposits - withdrawals after `since`."""

    def __init__(self, engine, max_age: float = 240):
        self.e = engine
        self.max_age = max_age
        self._cache: dict = {"ts": 0.0, "env": None, "rows": []}
        self.error = None

    @staticmethod
    def _t(x) -> float:
        x = float(x or 0)
        return x / 1000 if x > 1e11 else x

    def rows(self) -> list[tuple[float, float]]:
        env = self.e.cfg.env
        if time.time() - self._cache["ts"] <= self.max_age and self._cache["env"] == env:
            return self._cache["rows"]
        rows = []
        try:
            for path, key, sign in (("/portfolio/deposits", "deposits", 1), ("/portfolio/withdrawals", "withdrawals", -1)):
                for x in self.e.client.paginate(path, key, {"limit": 500}, auth=True, max_items=5000):
                    if x.get("status") != "applied":
                        continue
                    amt = float(x.get("amount_cents") or 0) / 100
                    if sign < 0:
                        amt += float(x.get("fee_cents") or 0) / 100       # a withdrawal fee also leaves the account
                    else:
                        amt -= float(x.get("fee_cents") or 0) / 100       # a deposit fee never arrives
                    rows.append((self._t(x.get("finalized_ts") or x.get("created_ts")), sign * amt))
            self.error = None
        except Exception as e:            # endpoint unavailable (e.g. not offered on demo): no adjustment
            self.error = str(e)[:200]
            rows = self._cache["rows"] if self._cache["env"] == env else []
        self._cache = {"ts": time.time(), "env": env, "rows": rows}
        return rows

    def net(self, since: float = 0.0) -> float:
        return sum(a for t, a in self.rows() if t >= since)
