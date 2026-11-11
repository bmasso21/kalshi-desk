"""Backtester (Phase 1.4): replays warehouse data market by market.

Timeline per market: hourly candles (decision points) + every public trade (fill evidence).
At each candle close the strategy sees ONLY data up to that moment (bars so far, trades so far,
best bid/ask at the close). The market's result is hidden until settlement.

Fills
  taker  fills immediately at the candle-close ask (+ `slippage` cents): buy YES at yes_ask,
         buy NO at 1 - yes_bid. Taker fee charged per fill.
  maker  rests at its limit price from the decision time until it expires / the market closes.
         A post-only order that would cross the close quote is rejected. It fills from LATER
         public trades, limited to each trade's size:
           through  (conservative) a trade printed strictly through your price
                    (YES bid p: trade at yes price < p; NO bid q: trade at yes price > 1 - q)
           touch    (optimistic) also trades AT your price where the taker was on your
                    counterparty's side (taker sold YES into bids / bought YES from asks)
         Neither knows your true queue position, so results are reported under BOTH models.
         Maker fee per the series' fee_type (0 on most series).
  fees   Kalshi schedule, rounded up to the cent per fill (conservative).

Settlement: YES pays $1 if result == yes, NO pays $1 if result == no. Scalar/void markets are skipped.
CLV: fill price vs the market's last traded YES price before close (the closing line).
"""
from __future__ import annotations

import json
import math
import statistics
import threading
import time
import traceback
from dataclasses import dataclass, field

from .fees import fee

FEE_TYPES_DEFAULT = ("quadratic", 1.0)


@dataclass
class Order:
    side: str           # 'yes' | 'no'  (what you buy)
    price: float        # your price for that side
    count: float
    maker: bool
    placed: float
    expire: float
    tag: str = ""
    filled: float = 0.0


@dataclass
class Fill:
    ticker: str
    ts: float
    side: str
    price: float
    count: float
    fee: float
    maker: bool
    tag: str = ""


class Ctx:
    """What a strategy can see and do at a decision point."""

    def __init__(self, sim, market: dict):
        self._sim = sim
        self.market = {k: v for k, v in market.items() if k not in ("result", "settlement_value", "settled_ts", "complete")}
        self.now = 0.0
        self.bar: dict | None = None
        self.bars: list[dict] = []
        self.params = sim.params
        self.state: dict = {}
        self.svc = sim.svc               # strategy-specific data service (e.g. the weather model), or None

    # data (never beyond self.now) -------------------------------------
    @property
    def yes_bid(self):
        return self.bar.get("bid_c") if self.bar else None

    @property
    def yes_ask(self):
        return self.bar.get("ask_c") if self.bar else None

    @property
    def hours_to_close(self) -> float:
        return ((self.market.get("close_ts") or self.now) - self.now) / 3600

    def trades(self) -> list[dict]:
        return [t for t in self._sim._trades if t["ts"] <= self.now]

    @property
    def position(self) -> dict:
        return {"yes": self._sim.pos["yes"], "no": self._sim.pos["no"]}

    @property
    def open_orders(self) -> list[Order]:
        return [o for o in self._sim.orders if o.filled < o.count and o.expire > self.now]

    # actions --------------------------------------------------------------
    def buy(self, side: str, price: float | None = None, count: float = 1, maker: bool = False,
            expire_hours: float | None = None, tag: str = "") -> bool:
        return self._sim.submit(side, price, count, maker, expire_hours, tag)

    def cancel_all(self):
        for o in self._sim.orders:
            o.expire = min(o.expire, self.now)


# ------------------------------------------------------------- strategies --
class Strategy:
    name = "base"
    description = ""
    defaults: dict = {}
    uses_maker = False
    candle_period = 60          # minutes per decision bar

    @classmethod
    def prepare(cls, wdb, params) -> object | None:
        """Optional: build a data service shared by every market in the run (exposed as ctx.svc)."""
        return None

    @classmethod
    def default_series(cls, svc) -> list[str] | None:
        """Optional: restrict the run to these series when the caller gave no series filter."""
        return None

    def on_bar(self, ctx: Ctx):
        raise NotImplementedError


class FavoriteLate(Strategy):
    name = "favorite_late"
    description = ("Taker. Once per market, at the first hourly close within `hours_before` hours of the market "
                   "closing, buy the side (YES or NO) whose ask is between `lo` and `hi`. Tests the "
                   "favourite-longshot bias; set lo/hi low (e.g. 0.03-0.10) to test longshots instead.")
    defaults = {"hours_before": 6, "lo": 0.80, "hi": 0.95, "count": 10, "side": "either"}

    def on_bar(self, ctx):
        p = ctx.params
        if ctx.state.get("done") or ctx.hours_to_close > float(p["hours_before"]) or ctx.hours_to_close <= 0:
            return
        ya, yb = ctx.yes_ask, ctx.yes_bid
        cands = []
        if p["side"] in ("either", "yes") and ya and 0 < ya < 1:
            cands.append(("yes", ya))
        if p["side"] in ("either", "no") and yb and 0 < yb < 1:
            cands.append(("no", round(1 - yb, 4)))
        for side, px in cands:
            if float(p["lo"]) <= px <= float(p["hi"]):
                ctx.buy(side, count=float(p["count"]), tag=side)
                break
        ctx.state["done"] = True


class MakerJoin(Strategy):
    name = "maker_join"
    description = ("Maker. Every hour, join the best YES bid and the best NO bid with post-only orders "
                   "(like the liquidity quoter, WITHOUT the rewards), stop adding to a side at `max_inventory`, "
                   "stop `pull_hours` before close, only when the spread is <= `max_spread`. Measures the "
                   "adverse-selection cost the rewards have to beat.")
    defaults = {"size": 10, "max_inventory": 30, "pull_hours": 6, "max_spread": 0.06, "min_mid": 0.15, "max_mid": 0.85}
    uses_maker = True

    def on_bar(self, ctx):
        p = ctx.params
        ctx.cancel_all()
        yb, ya = ctx.yes_bid, ctx.yes_ask
        if not yb or not ya or yb <= 0 or ya >= 1 or ctx.hours_to_close <= float(p["pull_hours"]):
            return
        mid = (yb + ya) / 2
        if ya - yb > float(p["max_spread"]) + 1e-9 or not float(p["min_mid"]) <= mid <= float(p["max_mid"]):
            return
        net = ctx.position["yes"] - ctx.position["no"]
        if net < float(p["max_inventory"]):
            ctx.buy("yes", yb, float(p["size"]), maker=True, expire_hours=1, tag="bid_yes")
        if -net < float(p["max_inventory"]):
            ctx.buy("no", round(1 - ya, 4), float(p["size"]), maker=True, expire_hours=1, tag="bid_no")


class WeatherFair(Strategy):
    name = "weather_fair"
    description = ("Maker (Phase 2.1). Every hour, price each daily high/low temperature bucket from NWS forecast "
                   "guidance (NBM) + today's observations + calibration from prior days only, then rest post-only "
                   "bids `edge` below fair value on YES and on NO. Starts `start_hours` before the climate day ends, "
                   "skips fair values outside [min_fair, max_fair]. Optional taker: if the ask is `take_edge` or "
                   "more below fair (after fees) take it (0 = never take).")
    defaults = {"edge": 0.05, "size": 10, "max_position": 30, "start_hours": 36, "min_fair": 0.03, "max_fair": 0.97,
                "take_edge": 0.0, "run_lag_h": 3.0, "calib_days": 60}
    uses_maker = True

    @classmethod
    def prepare(cls, wdb, params):
        from .weather import WxModel, WxStore
        return WxModel(WxStore(wdb), run_lag_h=float(params.get("run_lag_h", 3)),
                       calib_days=int(params.get("calib_days", 60)))

    @classmethod
    def default_series(cls, svc):
        return sorted(k for k, v in svc.smap.items() if v["mapped"])

    def on_bar(self, ctx):
        p = ctx.params
        ctx.cancel_all()
        fv = ctx.svc.fair(ctx.market, ctx.now) if ctx.svc else None
        if not fv or fv["hours_left"] > float(p["start_hours"]):
            return
        ctx.state["last_fair"] = fv["p_yes"]
        f_yes = fv["p_yes"]
        if not float(p["min_fair"]) <= f_yes <= float(p["max_fair"]):
            return
        yb, ya = ctx.yes_bid, ctx.yes_ask
        edge, size, cap = float(p["edge"]), float(p["size"]), float(p["max_position"])
        take = float(p["take_edge"])
        for side, fair, ask in (("yes", f_yes, ya if ya and 0 < ya < 1 else None),
                                ("no", 1 - f_yes, (1 - yb) if yb and 0 < yb < 1 else None)):
            room = cap - ctx.position[side]
            if room <= 0:
                continue
            if take > 0 and ask is not None and ask + 0.07 * ask * (1 - ask) <= fair - take:
                ctx.buy(side, price=round(ask, 2), count=min(size, room), tag=f"take_{side}")
                continue
            px = math.floor((fair - edge) * 100 + 1e-9) / 100
            if ask is not None:
                px = min(px, round(ask - 0.01, 2))
            if px >= 0.01:
                ctx.buy(side, px, min(size, room), maker=True, expire_hours=1, tag=f"bid_{side}")


class SportsFair(Strategy):
    name = "sports_fair"
    description = ("Maker (Phase 2.2). Pregame only: every `every_min` minutes from `start_minutes` until `stop_minutes` "
                   "before the game, price each Kalshi team market at Pinnacle's no-vig line as seen `lag_sec` seconds "
                   "earlier, and rest post-only bids `edge` below it on YES and NO (1-minute bars). Optional taker when the "
                   "ask is `take_edge` below fair after fees (0 = never take). Uses Kalshi's real sports maker fees.")
    defaults = {"edge": 0.02, "size": 10, "max_position": 50, "start_minutes": 360, "stop_minutes": 5, "every_min": 5,
                "lag_sec": 60, "take_edge": 0.0}
    uses_maker = True
    candle_period = 1

    @classmethod
    def prepare(cls, wdb, params):
        from .sports import SportsModel
        return SportsModel(wdb, lag_sec=float(params.get("lag_sec", 60)))

    @classmethod
    def default_series(cls, svc):
        return sorted({m.split("-")[0] for m in svc.by_market})

    def on_bar(self, ctx):
        p = ctx.params
        fv = ctx.svc.fair(ctx.market, ctx.now) if ctx.svc else None
        if not fv or not float(p["stop_minutes"]) <= fv["minutes_to_start"] <= float(p["start_minutes"]):
            ctx.cancel_all()
            return
        if ctx.now - ctx.state.get("last", 0) < float(p["every_min"]) * 60 - 1:
            return
        ctx.state["last"] = ctx.now
        ctx.cancel_all()
        yb, ya = ctx.yes_bid, ctx.yes_ask
        edge, size, cap, take = float(p["edge"]), float(p["size"]), float(p["max_position"]), float(p["take_edge"])
        for side, fair, ask in (("yes", fv["p_yes"], ya if ya and 0 < ya < 1 else None),
                                ("no", 1 - fv["p_yes"], (1 - yb) if yb and 0 < yb < 1 else None)):
            room = cap - ctx.position[side]
            if room <= 0:
                continue
            if take > 0 and ask is not None and ask + 0.07 * ask * (1 - ask) <= fair - take:
                ctx.buy(side, price=round(ask, 2), count=min(size, room), tag=f"take_{side}")
                continue
            px = math.floor((fair - edge) * 100 + 1e-9) / 100
            if ask is not None:
                px = min(px, round(ask - 0.01, 2))
            if px >= 0.01:
                ctx.buy(side, px, min(size, room), maker=True, expire_hours=float(p["every_min"]) / 60, tag=f"bid_{side}")


STRATEGIES: dict[str, type[Strategy]] = {s.name: s for s in (FavoriteLate, MakerJoin, WeatherFair, SportsFair)}


def register(cls: type[Strategy]):
    STRATEGIES[cls.name] = cls
    return cls


# --------------------------------------------------------------- simulator --
class MarketSim:
    def __init__(self, market: dict, bars: list[dict], trades: list[dict], strategy: Strategy, params: dict,
                 fee_type: str, fee_mult: float, maker_model: str, slippage: float, svc=None):
        self.svc = svc
        self.m, self.bars, self._trades = market, bars, trades
        self.strategy, self.params = strategy, params
        self.fee_type, self.fee_mult = fee_type or "quadratic", fee_mult if fee_mult is not None else 1.0
        self.model, self.slip = maker_model, slippage
        self.orders: list[Order] = []
        self.fills: list[Fill] = []
        self.pos = {"yes": 0.0, "no": 0.0}
        self.rejects = 0
        self.ctx = Ctx(self, market)

    def submit(self, side, price, count, maker, expire_hours, tag) -> bool:
        c = self.ctx
        if side not in ("yes", "no") or count <= 0 or not c.bar:
            self.rejects += 1
            return False
        ya, yb = c.yes_ask, c.yes_bid
        close_ts = self.m.get("close_ts") or c.now
        if maker:
            if price is None or not (0.01 <= price <= 0.99):
                self.rejects += 1
                return False
            ask_for_side = ya if side == "yes" else (1 - yb if yb else None)
            if ask_for_side is not None and ask_for_side > 0 and price >= ask_for_side - 1e-9:
                self.rejects += 1          # post-only would cross
                return False
            exp = min(close_ts, c.now + expire_hours * 3600) if expire_hours else close_ts
            self.orders.append(Order(side, round(price, 4), count, True, c.now, exp, tag))
            return True
        # taker: fill now at the close quote (+ slippage), capped by the strategy's limit if any
        px = (ya if side == "yes" else (1 - yb if yb else None))
        if px is None or not (0 < px < 1):
            self.rejects += 1
            return False
        px = round(min(0.99, px + self.slip), 4)
        if price is not None and px > price + 1e-9:
            self.rejects += 1
            return False
        self._fill(side, px, count, False, c.now, tag)
        return True

    def _fill(self, side, price, count, maker, ts, tag):
        fe = float(fee(price, count, self.fee_type, self.fee_mult, maker=maker))
        self.fills.append(Fill(self.m["ticker"], ts, side, price, count, fe, maker, tag))
        self.pos[side] += count

    def _match(self, t: dict):
        """Resting maker orders vs one public trade."""
        yp, qty = t["yes_price"], t["count"] or 0
        if yp is None or qty <= 0:
            return
        for o in self.orders:
            if qty <= 0:
                break
            rem = o.count - o.filled
            if rem <= 0 or o.placed >= t["ts"] or o.expire < t["ts"]:
                continue
            if o.side == "yes":
                through = yp < o.price - 1e-9
                at = abs(yp - o.price) < 1e-9 and t.get("taker_book_side") == "ask"
            else:
                through = yp > (1 - o.price) + 1e-9
                at = abs(yp - (1 - o.price)) < 1e-9 and t.get("taker_book_side") == "bid"
            if through or (self.model == "touch" and at):
                q = min(rem, qty)
                o.filled += q
                qty -= q
                self._fill(o.side, o.price, q, True, t["ts"], o.tag)

    def run(self) -> dict:
        ti = 0
        tr = self._trades
        for b in self.bars:
            end = b["end_ts"]
            while ti < len(tr) and tr[ti]["ts"] <= end:
                self._match(tr[ti])
                ti += 1
            if self.m.get("close_ts") and end > self.m["close_ts"] + 3600:
                break
            self.ctx.now, self.ctx.bar = float(end), b
            self.ctx.bars.append(b)
            self.strategy.on_bar(self.ctx)
        while ti < len(tr):                       # remaining trades until close can still fill resting orders
            if self.m.get("close_ts") and tr[ti]["ts"] > self.m["close_ts"]:
                break
            self._match(tr[ti])
            ti += 1
        return self.settle()

    def settle(self) -> dict:
        res = self.m.get("result")
        cost = sum(f.price * f.count for f in self.fills)
        fees = sum(f.fee for f in self.fills)
        payout = self.pos["yes"] * (1.0 if res == "yes" else 0.0) + self.pos["no"] * (1.0 if res == "no" else 0.0)
        close_px = self.closing_line()
        clv = []
        for f in self.fills:
            if close_px is not None:
                line = close_px if f.side == "yes" else 1 - close_px
                clv.append((line - f.price, f.count))
        return {"ticker": self.m["ticker"], "series": self.m.get("series_ticker"), "category": self.m.get("category"),
                "close_ts": self.m.get("close_ts"), "result": res, "fills": self.fills, "cost": cost, "fees": fees,
                "payout": payout, "pnl": payout - cost - fees, "contracts": sum(f.count for f in self.fills),
                "clv_sum": sum(c * n for c, n in clv), "clv_n": sum(n for _, n in clv), "rejects": self.rejects,
                "closing_line": close_px}

    def closing_line(self):
        ct = self.m.get("close_ts")
        last = [t for t in self._trades if t["yes_price"] is not None and (not ct or t["ts"] <= ct)]
        if last:
            return last[-1]["yes_price"]
        for b in reversed(self.bars):
            if b.get("px_c") is not None:
                return b["px_c"]
        return None


# ------------------------------------------------------------------ runner --
class Backtester:
    def __init__(self, wdb):
        self.db = wdb
        self.db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS wh_backtests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, strategy TEXT, params TEXT, filters TEXT,
            status TEXT, error TEXT, duration REAL, results TEXT
        );""")
        self.procs: dict = {}

    def load_markets(self, flt: dict) -> list[dict]:
        sql = ("SELECT m.*, s.fee_type, s.fee_multiplier FROM wh_markets m LEFT JOIN wh_series s "
               "ON s.series_ticker = m.series_ticker WHERE m.result IN ('yes','no')")
        args = []
        if flt.get("categories"):
            sql += f" AND m.category IN ({','.join('?' * len(flt['categories']))})"
            args += list(flt["categories"])
        if flt.get("series"):
            sql += f" AND m.series_ticker IN ({','.join('?' * len(flt['series']))})"
            args += list(flt["series"])
        if flt.get("start_ts"):
            sql += " AND m.close_ts >= ?"
            args.append(float(flt["start_ts"]))
        if flt.get("end_ts"):
            sql += " AND m.close_ts <= ?"
            args.append(float(flt["end_ts"]))
        if flt.get("min_volume"):
            sql += " AND COALESCE(m.volume,0) >= ?"
            args.append(float(flt["min_volume"]))
        sql += " ORDER BY m.close_ts LIMIT ?"
        args.append(int(flt.get("max_markets", 200000)))
        return self.db.query(sql, args)

    def market_data(self, ticker, period=60):
        bars = self.db.query("SELECT * FROM wh_candles WHERE ticker=? AND period=? ORDER BY end_ts", (ticker, period))
        trades = self.db.query("SELECT ts, yes_price, count, taker_book_side, taker_outcome_side FROM wh_trades "
                               "WHERE ticker=? ORDER BY ts, trade_id", (ticker,))
        return bars, trades

    def run(self, strategy: str, params: dict | None = None, filters: dict | None = None,
            models: list[str] | None = None, slippage_cents: float = 0, progress=None) -> dict:
        """Streams market by market (only one market's trades in memory at a time)."""
        cls = STRATEGIES[strategy]
        p = {**cls.defaults, **(params or {})}
        flt = dict(filters or {})
        models = models or (["through", "touch"] if cls.uses_maker else ["through"])
        svc = cls.prepare(self.db, p)
        if not flt.get("series") and svc is not None:
            ds = cls.default_series(svc)
            if ds is not None:
                flt["series"] = ds or ["__no_series_with_data__"]
        markets = self.load_markets(flt)
        per: dict[str, list] = {m: [] for m in models}
        for i, mk in enumerate(markets):
            bars, trades = self.market_data(mk["ticker"], cls.candle_period)
            if bars:
                for model in models:
                    sim = MarketSim(mk, bars, trades, cls(), p, mk.get("fee_type"), mk.get("fee_multiplier"), model,
                                    slippage_cents / 100, svc)
                    r = sim.run()
                    if not r["fills"]:
                        r = {k: v for k, v in r.items()}   # keep the light record only
                    per[model].append(r)
            if progress and i % 500 == 0:
                progress(i, len(markets))
        return {"strategy": strategy, "params": p, "filters": flt, "markets_in_scope": len(markets),
                "slippage_cents": slippage_cents, "models": {m: summarize(per[m]) for m in models}}

    # ---- stored runs, each in its own low-priority process ---------------------
    def start(self, strategy, params=None, filters=None, models=None, slippage_cents=0) -> int:
        """Queue a run and execute it in a separate, low-priority Python process, so a big backtest can
        never slow down or crash the trading app. Results land in wh_backtests."""
        if strategy not in STRATEGIES:
            raise ValueError(f"unknown strategy {strategy}")
        cur = self.db.execute("INSERT INTO wh_backtests(ts,strategy,params,filters,status,results) VALUES(?,?,?,?, 'running', ?)",
                              (time.time(), strategy, json.dumps(params or {}), json.dumps(filters or {}),
                               json.dumps({"models_requested": models, "slippage_cents": slippage_cents})))
        rid = cur.lastrowid
        import os
        import subprocess
        import sys
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        args = [sys.executable, "-m", "app.backtest", "--db", self.db.path, "--run", str(rid)]
        kw = {"cwd": str(root), "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            kw["creationflags"] = 0x00004000 | 0x08000000      # BELOW_NORMAL priority, no window
        self.procs[rid] = subprocess.Popen(args, **kw)
        return rid

    def execute_stored(self, rid: int):
        row = self.db.query("SELECT * FROM wh_backtests WHERE id=?", (rid,))[0]
        extra = json.loads(row["results"] or "{}")
        t0 = time.time()

        def prog(i, n):
            self.db.execute("UPDATE wh_backtests SET error=? WHERE id=? AND status='running'", (f"progress {i}/{n}", rid))
        try:
            r = self.run(row["strategy"], json.loads(row["params"] or "{}"), json.loads(row["filters"] or "{}"),
                         extra.get("models_requested"), float(extra.get("slippage_cents") or 0), progress=prog)
            self.db.execute("UPDATE wh_backtests SET status='done', error=NULL, duration=?, results=? WHERE id=?",
                            (round(time.time() - t0, 2), json.dumps(r, default=str), rid))
        except Exception as e:
            self.db.execute("UPDATE wh_backtests SET status='error', error=?, duration=? WHERE id=?",
                            (f"{e}\n{traceback.format_exc()[-1200:]}", round(time.time() - t0, 2), rid))


def summarize(per: list[dict]) -> dict:
    traded = [r for r in per if r["fills"]]
    tot = lambda k: sum(r[k] for r in traded)
    cost, fees, payout, pnl = tot("cost"), tot("fees"), tot("payout"), tot("pnl")
    contracts = tot("contracts")
    clv_n = tot("clv_n")
    # P&L by settlement day -> equity curve, drawdown, daily Sharpe
    by_day: dict[str, float] = {}
    for r in sorted(traded, key=lambda r: r["close_ts"] or 0):
        day = time.strftime("%Y-%m-%d", time.gmtime(r["close_ts"] or 0))
        by_day[day] = by_day.get(day, 0.0) + r["pnl"]
    eq, peak, mdd, curve = 0.0, 0.0, 0.0, []
    for day, v in sorted(by_day.items()):
        eq += v
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
        curve.append([day, round(eq, 2)])
    daily = list(by_day.values())
    sharpe = (statistics.mean(daily) / statistics.pstdev(daily) * math.sqrt(365)) if len(daily) > 1 and statistics.pstdev(daily) > 0 else None

    def group(key):
        g: dict[str, dict] = {}
        for r in traded:
            k = r.get(key) or "?"
            x = g.setdefault(k, {"markets": 0, "contracts": 0.0, "cost": 0.0, "fees": 0.0, "pnl": 0.0, "clv_sum": 0.0, "clv_n": 0.0})
            x["markets"] += 1
            for f_ in ("contracts", "cost", "fees", "pnl", "clv_sum", "clv_n"):
                x[f_] += r[f_]
        return {k: {**v, "roi": (v["pnl"] / v["cost"]) if v["cost"] else None,
                    "avg_clv_cents": (100 * v["clv_sum"] / v["clv_n"]) if v["clv_n"] else None}
                for k, v in sorted(g.items(), key=lambda kv: -kv[1]["pnl"])}

    fills = [f for r in traded for f in r["fills"]]
    return {
        "markets_simulated": len(per), "markets_traded": len(traded), "fills": len(fills),
        "maker_fills": sum(1 for f in fills if f.maker), "contracts": round(contracts, 2),
        "cost": round(cost, 2), "fees": round(fees, 2), "payout": round(payout, 2), "pnl": round(pnl, 2),
        "roi": round(pnl / cost, 4) if cost else None,
        "pnl_per_contract_cents": round(100 * pnl / contracts, 2) if contracts else None,
        "win_rate_markets": round(sum(1 for r in traded if r["pnl"] > 0) / len(traded), 4) if traded else None,
        "avg_clv_cents": round(100 * tot("clv_sum") / clv_n, 2) if clv_n else None,
        "max_drawdown": round(mdd, 2), "sharpe_daily_annualized": round(sharpe, 2) if sharpe is not None else None,
        "days": len(by_day), "rejected_orders": sum(r["rejects"] for r in per),
        "equity_curve": curve, "by_category": group("category"), "by_series": group("series"),
        "sample_fills": [{"ticker": f.ticker, "ts": f.ts, "side": f.side, "price": f.price, "count": f.count,
                          "fee": f.fee, "maker": f.maker} for f in fills[:200]],
        "per_market": [{"ticker": r["ticker"], "result": r["result"], "contracts": r["contracts"], "cost": round(r["cost"], 2),
                        "fees": round(r["fees"], 2), "pnl": round(r["pnl"], 2), "closing_line": r["closing_line"]}
                       for r in traded[:500]],
    }


if __name__ == "__main__":
    import argparse

    from .warehouse import WarehouseDB
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--run", type=int, required=True)
    a = ap.parse_args()
    Backtester(WarehouseDB(a.db)).execute_stored(a.run)
