"""Liquidity-reward quoter.

Kalshi's Liquidity Incentive Program pays resting orders near the top of the book, filled
or not (scored on random snapshots every second). This module:
  * finds markets with active liquidity programs and a pool worth quoting,
  * joins the best YES bid and the best NO bid with post-only orders (maker -> $0 fee on most series),
  * stops adding to a side once net inventory hits max_inventory (YES and NO fills net out),
  * pulls quotes when the mid jumps (news) and cools down, and stops before settlement (no in-game),
  * caps total $ committed (resting quotes + inventory),
  * gives every quote an expiry (quote_ttl_sec, default 10 min) so nothing is left resting if the app or laptop
    dies; it is re-placed only shortly before that, so it keeps its place in the queue.

The main risk is adverse selection: you mostly get filled right before the price moves against
you. Rewards must beat that cost; judge it on the Rewards tab over weeks, not days.
"""
from __future__ import annotations

import collections
import datetime as dt
import threading
import time
import traceback
from decimal import Decimal

from . import mm, risk
from .kalshi_client import KalshiError, d

ONE = Decimal(1)


def _ts(iso):
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Quoter:
    def __init__(self, engine):
        self.e = engine
        self.quotes: dict[str, dict[str, dict]] = {}   # ticker -> side('yes'/'no') -> {oid, price, exp, client}
        self.selected: list[dict] = []                  # [{ticker, daily_reward, title}]
        self.programs_ts = 0
        self.last_mid: dict[str, Decimal] = {}
        self.cooldown: dict[str, float] = {}
        self.events = collections.deque(maxlen=300)
        self.state = {"running": False, "last": None, "error": None, "markets": 0, "resting": 0,
                      "committed": "0"}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.RLock()
        self.mid_hist: dict[str, collections.deque] = {}
        self.last_cycle = 0.0
        self.cycles = 0

    # ------------------------------------------------------------ helpers --
    def log(self, kind, msg):
        self.events.appendleft({"ts": time.time(), "kind": kind, "msg": msg})

    @property
    def cfg(self):
        return self.e.cfg.quoter

    def active(self) -> bool:
        c = self.e.cfg
        return bool(self.cfg.get("enabled")) and not c.risk.get("kill_switch") and self.e.client.authenticated

    def cancel_all(self, reason="stop"):
        with self._lock:
            n = 0
            for t, sides in list(self.quotes.items()):
                for side in list(sides):
                    if self._cancel(t, side):
                        n += 1
            self.quotes.clear()
        if n:
            self.log("cancel_all", f"{reason}: cancelled {n} quotes")
        return n

    def _cancel(self, ticker, side) -> bool:
        q = self.quotes.get(ticker, {}).pop(side, None)
        if not q:
            return False
        try:
            q["client"].cancel_order(q["oid"], ticker)
            return True
        except KalshiError as e:
            if e.status != 404:  # 404 = already filled/expired/cancelled
                self.log("cancel_error", f"{ticker} {side}: {e}")
            return False

    def on_feed(self, kind, msg):
        """Wake the quoter when a quoted market's book or one of our orders changes."""
        if not self.cfg.get("enabled"):
            return
        if kind == "book" and msg.get("ticker") in self.quotes:
            self._wake.set()
        elif kind in ("order", "fill"):
            t = msg.get("ticker") or msg.get("market_ticker")
            if t in self.quotes:
                self._wake.set()

    def _jumped(self, t, mid, now) -> Decimal | None:
        """Mid moved >= jump_cents vs. any mid seen in the last refresh window (and always vs. the
        previous cycle's mid), so faster, feed-driven cycles don't dilute the jump test."""
        h = self.mid_hist.setdefault(t, collections.deque(maxlen=200))
        window = float(self.cfg.get("refresh_sec") or 15)
        ref = [m for ts, m in h if now - ts <= window]
        if h:
            ref.append(h[-1][1])
        h.append((now, mid))
        thr = d(self.cfg["jump_cents"]) / 100
        worst = max(ref, key=lambda m: abs(mid - m)) if ref else None
        if worst is not None and abs(mid - worst) >= thr:
            h.clear()
            h.append((now, mid))   # the new level is the reference after the cooldown
            return worst
        return None

    # ---------------------------------------------------------- selection --
    def refresh_programs(self):
        self.selected, msg = self.select(self.e.client)
        self.programs_ts = time.time()
        self.log("select", msg)

    def _refresh_bg(self):
        """Re-pick markets in the background (it takes ~30s at a polite request rate), so the cycle keeps
        watching the live quotes for price jumps meanwhile. The new list applies on the next cycle."""
        if getattr(self, "_picking", False):
            return
        self._picking = True
        self.programs_ts = time.time()

        def run():
            try:
                self.refresh_programs()
            except Exception as e:
                self.log("error", f"re-pick failed (keeping the current markets): {e}")
            finally:
                self._picking = False
                self._wake.set()
        threading.Thread(target=run, daemon=True, name="quoter-pick").start()

    def select(self, client):
        """Pick markets to quote. Read-only: public data only. Every market with an active liquidity program
        that passes the quoter's filters is scored with Kalshi's formula at your quote size (app/mm.py); the
        best estimated reward per $ of capital is taken first until `max_markets` or `max_capital`."""
        c = self.cfg
        size = int(d(c["size"]))
        rules = {k: c.get(k) for k in ("pull_hours", "max_spread", "min_mid", "max_mid")}
        rows, info = mm.rank_markets(client, rules, (size,), time.time(), prefilter=True,
                                     min_pool=float(c.get("min_daily_reward") or 0), max_rps=4)
        picked = mm.pick_for_quoter(rows, size, int(c["max_markets"]), float(c["max_capital"]),
                                    float(c.get("max_volume_24h") or 0))
        k = str(size)
        chosen = [{"ticker": r["ticker"], "daily_reward": d(r["daily"]), "est_daily": d(r["sizes"][k]["est_daily"]),
                   "share": d((r["sizes"][k]["share_yes"] + r["sizes"][k]["share_no"]) / 2),
                   "capital": d(r["sizes"][k]["capital"]), "title": r.get("title"), "sub": r.get("sub")} for r in picked]
        paying = sum(1 for r in rows if r["sizes"][k]["est_daily"] > 0)
        return chosen, (f"{info['programs']} active programs; {len(info['pools'])} pools >= ${c['min_daily_reward']}/day; "
                        f"{len(rows)} pass the filters; {paying} would pay you >= Kalshi's $1 minimum at {size} contracts; "
                        f"quoting {len(chosen)} (capital ${sum(x['capital'] for x in chosen):.2f} of ${c['max_capital']}): "
                        + ", ".join(f"{x['ticker']} (~${x['est_daily']:.2f}/d of ${x['daily_reward']:.0f})" for x in chosen))

    def _eligible(self, m) -> tuple[bool, str] | bool:
        c = self.cfg
        if m.get("status") != "active" or (m.get("market_type") or "binary") != "binary":
            return False
        ends = [x for x in (_ts(m.get("expected_expiration_time")), _ts(m.get("close_time"))) if x]
        if ends and min(ends) - time.time() < float(c["pull_hours"]) * 3600:
            return False
        yb, ya = d(m.get("yes_bid_dollars")), d(m.get("yes_ask_dollars"))
        if yb <= 0 or ya <= 0 or ya - yb > d(c["max_spread"]):
            return False
        mid = (yb + ya) / 2
        return d(c["min_mid"]) <= mid <= d(c["max_mid"])

    # -------------------------------------------------------------- cycle --
    def cycle(self):
        if not self.active():
            if self.quotes:
                self.cancel_all("quoter disabled / kill switch")
            if getattr(self.e, "feed", None):
                self.e.feed.want("quoter", ())
            return
        self.e.check_loss_limit()
        if not self.active():
            self.cancel_all("loss limit")
            return
        if not self.programs_ts:
            self.refresh_programs()                 # first pick: nothing is quoted yet, so do it inline
        elif time.time() - self.programs_ts > 600:
            self._refresh_bg()
        client = self.e.client
        sel = {s["ticker"] for s in self.selected}
        # drop quotes in markets no longer selected
        for t in [t for t in self.quotes if t not in sel]:
            for side in list(self.quotes[t]):
                self._cancel(t, side)
            self.quotes.pop(t, None)
        if not sel:
            self._update_state(client)
            return
        feed = getattr(self.e, "feed", None)
        if feed:
            feed.want("quoter", sel)
        markets = {m["ticker"]: (feed.market_overlay(m) if feed else m)
                   for m in client.markets(status=None, tickers=sorted(sel), exclude_combos=False)}
        live_orders = feed.resting_orders() if feed else None
        resting = {o["order_id"]: o for o in (live_orders if live_orders is not None
                                               else client.orders(status="resting", max_items=1000))}
        positions = self.e.positions(max_age=5) or {"market_positions": []}
        held = {mp["ticker"]: d(mp.get("position_fp")) for mp in positions.get("market_positions", [])}
        exposure = {mp["ticker"]: abs(d(mp.get("market_exposure_dollars"))) for mp in positions.get("market_positions", [])}
        spent = self.e.spent_today(max_age=5)
        now = time.time()

        # forget quotes that are no longer resting (filled or expired)
        for t in list(self.quotes):
            for side, q in list(self.quotes[t].items()):
                if q["oid"] not in resting:
                    if now - q.get("placed", 0) < 10:
                        continue  # just placed; the feed/REST order list may not show it yet
                    del self.quotes[t][side]
                    self.log("gone", f"{t} {side} @{q['price']} no longer resting (filled or expired)")

        committed = sum((exposure.get(t, Decimal(0)) for t in sel), Decimal(0)) + sum(
            (d(q["price"]) * d(self.cfg["size"]) for sides in self.quotes.values() for q in sides.values()), Decimal(0))

        for t in sorted(sel):
            m = markets.get(t)
            if not m or not self._eligible(m):
                for side in list(self.quotes.get(t, {})):
                    self._cancel(t, side)
                continue
            yb, ya = d(m["yes_bid_dollars"]), d(m["yes_ask_dollars"])
            mid = (yb + ya) / 2
            last = self._jumped(t, mid, now)
            self.last_mid[t] = mid
            if last is not None:
                for side in list(self.quotes.get(t, {})):
                    self._cancel(t, side)
                self.cooldown[t] = now + float(self.cfg["cooldown_min"]) * 60
                self.log("jump", f"{t} mid moved {last:.2f} -> {mid:.2f}; pulled quotes, cooling down")
                continue
            if self.cooldown.get(t, 0) > now:
                continue
            pos = held.get(t, Decimal(0))
            inv = d(self.cfg["max_inventory"])
            want = {}
            if pos < inv:
                want["yes"] = ("buy_yes", yb)               # join best YES bid
            if -pos < inv:
                want["no"] = ("buy_no", ONE - ya)           # join best NO bid (= YES ask)
            for side in ("yes", "no"):
                q = self.quotes.get(t, {}).get(side)
                if side not in want:
                    if q:
                        self._cancel(t, side)
                        self.log("inventory", f"{t}: net position {pos}; stopped quoting {side.upper()}")
                    continue
                intent, px = want[side]
                if q and d(q["price"]) == px and q["exp"] - now > 60:
                    continue  # still at the top and not about to expire
                if q:
                    self._cancel(t, side)
                    committed -= d(q["price"]) * d(self.cfg["size"])
                add = px * d(self.cfg["size"])
                if committed + add > d(self.cfg["max_capital"]):
                    continue
                # longer-lived quotes keep their place in the queue; still expire on their own if the app dies
                exp = int(now + max(120, int(self.cfg.get("quote_ttl_sec") or 600), int(self.cfg["refresh_sec"]) * 6))
                try:
                    r = self.e.place(ticker=t, intent=intent, price=px, count=self.cfg["size"], source="quoter",
                                     strategy="liquidity", time_in_force="good_till_canceled", post_only=True,
                                     expiration_time=exp, market=m)
                    k = r["kalshi"]
                    if d(k.get("remaining_count")) > 0:
                        self.quotes.setdefault(t, {})[side] = {"oid": k["order_id"], "price": str(px), "exp": exp,
                                                               "client": client, "placed": time.time()}
                        committed += add
                    if d(k.get("fill_count")) > 0:
                        self.log("fill", f"{t} {intent} {k['fill_count']} @ {px} filled on placement")
                except risk.RiskError as e:
                    self.log("blocked", f"{t} {intent} @{px}: {e}")
                except KalshiError as e:
                    self.log("rejected", f"{t} {intent} @{px}: {e}")
        self._update_state(client, committed)

    def _update_state(self, client, committed=Decimal(0)):
        self.state.update(last=time.time(), error=None, markets=len(self.selected),
                          resting=sum(len(s) for s in self.quotes.values()), committed=str(committed.quantize(Decimal("0.01"))))

    MIN_GAP = 2.0   # seconds between feed-triggered cycles

    def loop(self):
        while not self._stop.is_set():
            self.state["running"] = self.active()
            self._wake.clear()
            try:
                with self._lock:
                    self.last_cycle = time.time()
                    self.cycles += 1
                    self.cycle()
            except Exception as e:
                self.state["error"] = str(e)
                self.log("error", traceback.format_exc()[-600:])
                self.e.db.audit("quoter_error", traceback.format_exc()[-600:])
            # sleep until the refresh interval, or earlier if the live feed reports a change
            self._wake.wait(max(5, int(self.cfg.get("refresh_sec") or 15)))
            gap = self.MIN_GAP - (time.time() - self.last_cycle)
            if gap > 0:
                self._stop.wait(gap)

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="quoter").start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        self.cancel_all("shutdown")

    def _fmt(self, rows):
        q2 = Decimal("0.01")
        return [{**s, "daily_reward": str(s["daily_reward"].quantize(q2)), "est_daily": str(s["est_daily"].quantize(q2)),
                 "share": str((s["share"] * 100).quantize(Decimal("0.1"))),
                 "capital": str(d(s.get("capital")).quantize(q2))} for s in rows]

    def snapshot(self):
        return {"state": self.state, "selected": self._fmt(self.selected),
                "quotes": [{"ticker": t, "side": s, "price": q["price"], "expires": q["exp"]}
                           for t, sides in self.quotes.items() for s, q in sides.items()],
                "events": list(self.events)[:100]}
