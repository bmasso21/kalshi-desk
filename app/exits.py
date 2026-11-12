"""Exit rules (v3.2): per-position take-profit and stop-loss. Closing orders only - a rule can never open a position.

A rule belongs to one market in the current environment and to the side you hold (YES or NO) when you set it:
  take_profit  sell everything when the BID for your side is at or above this price
  stop_loss    sell everything when the BID for your side is at or below this price
  floor        (optional, with stop_loss) never sell below this price - protects against a thin book; if the bid
               is under the floor the rule waits and you are alerted once

Prices are in your side's terms (a NO position uses NO prices; NO bid = 1 - YES ask). Checked every `check_sec`
seconds against Kalshi's quoted best bid; the condition must hold on `confirm_checks` checks in a row (default 2), so
one thin quote can't trigger a sale. Before selling it cancels every order of yours resting in that market (so two sells
can never exceed the position). It then sends an immediate-or-cancel sell at the bid for the
whole position (in chunks of the max contracts per order); whatever doesn't fill is retried on the next check while
the condition still holds. No bid at all = nothing to sell into, so nothing is sent. The rule ends when the position
is flat, flips side, or the market closes. Sells are always allowed by the risk limits and work with the kill switch on.

A stop-loss sells at whatever the bid is: in a thin market that can be well below fair value. Use `floor` to bound it.

Saving a rule (v3.5): a level that the current bid has already passed is refused - a stop-loss at or above the bid, or a
take-profit at or below it, would fire on the next check - with the bid shown in the message.

Suggested levels (v3.5): `suggest(ticker)` proposes take / stop / floor from the position itself. It is risk
arithmetic, NOT a forecast - it says nothing about whether the trade is good:
  stop   = bid - distance, distance = the largest of 3c, twice the bid/ask spread, and the move since yesterday's close
           (so normal noise doesn't trigger it); tightened if the loss at the stop would exceed half the daily loss limit
  floor  = stop - the larger of 2c and the spread (room for a thin book)
  take   = entry + 1.5 x (entry - stop), capped at 0.99 and always above the bid
"""
from __future__ import annotations

import threading
import time
import traceback

from . import risk
from .kalshi_client import KalshiError, d

SCHEMA = """
CREATE TABLE IF NOT EXISTS exit_rules (
    env TEXT, ticker TEXT, side TEXT, take_profit REAL, stop_loss REAL, floor REAL, created REAL,
    status TEXT, note TEXT, updated REAL, PRIMARY KEY (env, ticker)
);
"""


class Exits:
    def __init__(self, engine):
        self.e = engine
        self.db = engine.db
        with self.db.lock:
            self.db.conn.executescript(SCHEMA)
        self.state = {"last": None, "error": None}
        self._stop = threading.Event()
        self._warned: set = set()
        self._seen: dict[str, int] = {}

    @property
    def cfg(self) -> dict:
        return self.e.cfg.data.setdefault("exits", {})

    # --------------------------------------------------------------- rules --
    def rules(self, active_only=True) -> list[dict]:
        sql = "SELECT * FROM exit_rules WHERE env=?" + (" AND status='active'" if active_only else "") + " ORDER BY created DESC"
        return self.db.query(sql, (self.e.cfg.env,))

    def _mp(self, ticker) -> dict:
        pos = self.e.positions(max_age=0) or {}
        for mp in pos.get("market_positions", []):
            if mp.get("ticker") == ticker:
                return mp
        return {}

    def _pos(self, ticker) -> float:
        return float(d(self._mp(ticker).get("position_fp")))

    def _quote(self, ticker: str, side: str) -> dict:
        """Best bid / ask and yesterday's close for the side held (NO prices for a NO position); None where missing."""
        try:
            ms = self.e.client.markets(status=None, tickers=[ticker], exclude_combos=False)
        except Exception:
            ms = []
        m = next((x for x in ms if x.get("ticker") == ticker), None) or {}
        yb, ya, pv = float(d(m.get("yes_bid_dollars"))), float(d(m.get("yes_ask_dollars"))), float(d(m.get("previous_price_dollars")))
        if side == "yes":
            bid, ask, prev = yb, ya, pv
        else:
            bid, ask, prev = (1 - ya if ya > 0 else 0.0), (1 - yb if yb > 0 else 0.0), (1 - pv if pv > 0 else 0.0)
        ok = lambda x: round(x, 4) if 0 < x < 1 else None
        return {"bid": ok(bid), "ask": ok(ask), "prev": ok(prev), "status": m.get("status")}

    def suggest(self, ticker: str) -> dict:
        mp = self._mp(ticker)
        pos = float(d(mp.get("position_fp")))
        if pos == 0:
            raise ValueError("you hold no position in that market")
        side, qty = ("yes" if pos > 0 else "no"), abs(pos)
        q = self._quote(ticker, side)
        bid = q["bid"]
        if bid is None:
            raise ValueError(f"no {side.upper()} bid in that market right now - nothing to base a suggestion on")
        cost = float(d(mp.get("market_exposure_dollars")))
        entry = round(cost / qty, 4) if cost > 0 else bid
        entry = min(max(entry, 0.01), 0.99)
        spread = round(q["ask"] - bid, 4) if q["ask"] and q["ask"] > bid else 0.01
        move = abs(bid - q["prev"]) if q["prev"] else 0.0
        dist = max(0.03, 2 * spread, move)
        notes = []
        cap = float(self.e.cfg.risk.get("max_daily_loss") or 0) / 2
        stop = bid - dist
        if cap > 0 and (entry - stop) * qty > cap:
            tight = entry - cap / qty
            if tight < bid - 0.02 + 1e-9:
                stop = max(stop, tight)
                notes.append(f"Stop tightened so the loss at the stop stays near ${cap:.2f} (half your daily loss limit).")
            else:
                stop = bid - 0.02
                notes.append(f"This position is large for your limits: even a stop 2c under the bid loses about "
                             f"${(entry - stop) * qty:.2f}, more than half your daily loss limit (${cap:.2f}).")
        stop = round(max(0.01, int(stop * 100 + 1e-6) / 100), 2)
        if stop >= bid:
            raise ValueError(f"the {side.upper()} bid ({bid:.2f}) is too low to place a stop under it")
        floor = round(max(0.01, stop - max(0.02, spread)), 2)
        risk_c = max(entry - stop, 0.01)
        take = min(0.99, entry + 1.5 * risk_c)
        take = round(max(take, bid + 0.01), 2)
        take = min(take, 0.99)
        if take <= bid:
            take = None
            notes.append("The bid is already at 0.99: there is no room for a take-profit.")
        gain_c = (take - entry) if take else 0.0
        if take and gain_c < risk_c - 1e-9:
            notes.append(f"At this entry the most the take-profit can earn is {gain_c * 100:.0f}c a contract against "
                         f"{risk_c * 100:.0f}c at risk to the stop - the price is too close to $1 for a better ratio.")
        if stop >= entry:
            notes.append("The stop is above what you paid: it locks in a gain rather than limiting a loss.")
        return {"ticker": ticker, "side": side, "qty": qty, "entry": round(entry, 4), "bid": bid, "ask": q["ask"], "spread": spread,
                "take_profit": take, "stop_loss": stop, "floor": floor,
                "at_stop": round((stop - entry) * qty, 2), "at_take": round(gain_c * qty, 2) if take else None,
                "basis": f"stop {dist * 100:.0f}c under the bid (largest of 3c, 2x the {spread * 100:.0f}c spread, "
                         f"{move * 100:.0f}c move since yesterday); floor allows a thin book; take-profit 1.5x the risk",
                "notes": notes,
                "disclaimer": "Risk arithmetic from your position and limits - not a forecast and not a view on whether the trade is good."}

    def set_rule(self, ticker: str, take_profit=None, stop_loss=None, floor=None, check_prices: bool = True) -> dict:
        def num(x, name):
            if x in (None, "", 0, "0"):
                return None
            v = round(float(x), 4)
            if not 0.01 <= v <= 0.99:
                raise ValueError(f"{name} must be between 0.01 and 0.99")
            return v
        tp, sl, fl = num(take_profit, "take-profit"), num(stop_loss, "stop-loss"), num(floor, "floor")
        if tp is None and sl is None:
            raise ValueError("set a take-profit, a stop-loss, or both")
        if tp is not None and sl is not None and tp <= sl:
            raise ValueError("take-profit must be above the stop-loss")
        if fl is not None and (sl is None or fl > sl):
            raise ValueError("floor needs a stop-loss and must be at or below it")
        pos = self._pos(ticker)
        if pos == 0:
            raise ValueError("you hold no position in that market")
        side = "yes" if pos > 0 else "no"
        if check_prices:
            bid = self._quote(ticker, side)["bid"]
            if bid is not None:                 # no bid = nothing to compare with (and nothing would be sold anyway)
                S = side.upper()
                if sl is not None and sl >= bid - 1e-9:
                    raise ValueError(f"stop-loss {sl:.2f} is at or above the current {S} bid ({bid:.2f}), so it would sell "
                                     f"right away. A stop-loss goes BELOW the current price. Use Suggest for levels.")
                if tp is not None and tp <= bid + 1e-9:
                    raise ValueError(f"take-profit {tp:.2f} is at or below the current {S} bid ({bid:.2f}), so it would sell "
                                     f"right away. A take-profit goes ABOVE the current price. Use Suggest for levels.")
        now = time.time()
        self.db.execute("INSERT OR REPLACE INTO exit_rules VALUES(?,?,?,?,?,?,?, 'active', NULL, ?)",
                        (self.e.cfg.env, ticker, side, tp, sl, fl, now, now))
        self.db.audit("exit_rule", f"{ticker} {side.upper()}: take-profit {tp}, stop-loss {sl}, floor {fl}")
        return {"ticker": ticker, "side": side, "take_profit": tp, "stop_loss": sl, "floor": fl}

    def remove(self, ticker: str) -> bool:
        cur = self.db.execute("DELETE FROM exit_rules WHERE env=? AND ticker=?", (self.e.cfg.env, ticker))
        return cur.rowcount > 0

    def _end(self, r, status, note):
        self.db.execute("UPDATE exit_rules SET status=?, note=?, updated=? WHERE env=? AND ticker=?",
                        (status, note, time.time(), r["env"], r["ticker"]))

    # --------------------------------------------------------------- check --
    def check(self) -> list[dict]:
        if not self.cfg.get("enabled", True) or not self.e.client or not self.e.client.authenticated:
            return []
        rules = self.rules()
        if not rules:
            self.state.update(last=time.time(), error=None)
            return []
        pos = self.e.positions(max_age=0) or {}
        held = {mp["ticker"]: float(d(mp.get("position_fp"))) for mp in pos.get("market_positions", [])}
        mk = {m["ticker"]: m for m in self.e.client.markets(status=None, tickers=[r["ticker"] for r in rules], exclude_combos=False)}
        out = []
        for r in rules:
            t, p, m = r["ticker"], held.get(r["ticker"], 0.0), mk.get(r["ticker"])
            if p == 0 or (p > 0) != (r["side"] == "yes"):
                self._end(r, "closed", "position closed or changed side")
                continue
            if not m or m.get("status") not in ("active", "open"):
                if m and (m.get("result") or m.get("status") in ("closed", "settled", "finalized", "determined")):
                    self._end(r, "closed", f"market {m.get('status')}")
                continue
            bid = float(d(m.get("yes_bid_dollars"))) if r["side"] == "yes" else \
                (1 - float(d(m.get("yes_ask_dollars"))) if float(d(m.get("yes_ask_dollars"))) > 0 else 0.0)
            if not 0 < bid < 1:
                continue                                    # no bid: nothing to sell into
            why = None
            if r["take_profit"] is not None and bid >= r["take_profit"] - 1e-9:
                why = f"take-profit {r['take_profit']:.2f} reached (bid {bid:.2f})"
            elif r["stop_loss"] is not None and bid <= r["stop_loss"] + 1e-9:
                if r["floor"] is not None and bid < r["floor"] - 1e-9:
                    if t not in self._warned:
                        self._warned.add(t)
                        self.db.audit("EXIT", f"{t}: stop-loss {r['stop_loss']:.2f} hit but the bid {bid:.2f} is below your "
                                              f"floor {r['floor']:.2f} - NOT sold. Waiting for a bid at or above the floor.")
                    continue
                why = f"stop-loss {r['stop_loss']:.2f} hit (bid {bid:.2f})"
            if not why:
                self._seen.pop(t, None)
                continue
            # one thin quote must not trigger a sale: the condition has to hold on consecutive checks
            need = max(1, int(self.cfg.get("confirm_checks") or 2))
            self._seen[t] = self._seen.get(t, 0) + 1
            if self._seen[t] < need:
                continue
            # cancel everything of ours resting in this market first, so this sell plus a resting sell (the quoter's
            # exit quote, or one you placed) can never add up to more than the position
            try:
                for o in self.e.client.orders(status="resting", ticker=t, max_items=200):
                    if o.get("ticker") in (None, t):
                        try:
                            self.e.client.cancel_order(o["order_id"], t)
                        except KalshiError:
                            pass
                if getattr(self.e, "quoter", None):
                    self.e.quoter.quotes.pop(t, None)
            except Exception as e:
                self.db.audit("exit_error", f"{t}: could not clear resting orders before the exit ({e}); not selling this check")
                continue
            p = self._pos(t)                                # re-read: a resting order may have filled meanwhile
            if p == 0 or (p > 0) != (r["side"] == "yes"):
                self._end(r, "closed", "position closed or changed side")
                continue
            left, sold = abs(p), 0.0
            chunk = float(d(self.e.cfg.risk["max_contracts_per_order"]))
            intent = "sell_yes" if r["side"] == "yes" else "sell_no"
            err = None
            while left > 0:
                n = min(left, chunk)
                try:
                    res = self.e.place(ticker=t, intent=intent, price=round(bid, 2), count=n, source="exit",
                                       strategy="exit_rule", time_in_force="immediate_or_cancel", market=m)
                    f = float(d(res["kalshi"].get("fill_count")))
                except (risk.RiskError, KalshiError) as e:
                    err, f = str(e), 0.0
                sold += f
                left -= n
                if f < n:
                    break                                   # book exhausted at this price; retry next check
            self._warned.discard(t)
            self._seen.pop(t, None)
            remaining = abs(p) - sold
            if remaining <= 1e-9:
                self._end(r, "triggered", f"{why}: sold {sold:g} @ {bid:.2f}")
            self.db.audit("EXIT", f"{t} {r['side'].upper()}: {why}. Sold {sold:g} of {abs(p):g} at {bid:.2f}"
                                  + (f"; {remaining:g} left, retrying" if remaining > 1e-9 else "")
                                  + (f" (error: {err})" if err else ""))
            out.append({"ticker": t, "why": why, "sold": sold, "remaining": remaining, "error": err})
        self.state.update(last=time.time(), error=None)
        return out

    def loop(self):
        while not self._stop.is_set():
            try:
                self.check()
            except Exception as e:
                self.state["error"] = str(e)[:300]
                self.db.audit("exit_error", traceback.format_exc()[-600:])
            self._stop.wait(max(10, float(self.cfg.get("check_sec") or 30)))

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="exits").start()

    def stop(self):
        self._stop.set()
