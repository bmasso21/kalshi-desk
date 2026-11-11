# Kalshi Desk

A private trading desk for Kalshi that runs on your Windows laptop. Browse every open Kalshi
market, scan for edges, place and cancel orders, track positions/fills/settlements, and
optionally let it auto-trade inside hard limits.

Built against Kalshi's Trade API v2 (OpenAPI spec v3.35.0, Nov 2026) using the V2 order
endpoint (`POST /portfolio/events/orders`). Tested end-to-end against a strict mock of the API
with both Ed25519 and RSA keys (70 checks).

## 1. Install (one time)

1. Install **Python 3.11+** from https://www.python.org/downloads/ - tick **"Add python.exe to PATH"**.
2. Unzip this folder somewhere permanent, e.g. `C:\KalshiDesk`.
3. Double-click **setup.bat**.

## 2. Get a DEMO key first (fake money)

1. Make a demo account at https://demo.kalshi.co (separate from your real account).
2. In demo: profile/account settings -> **API Keys** -> **Create New API Key**.
3. Save the private key file into `C:\KalshiDesk\keys\demo.key` and copy the **Key ID**.
   The private key is shown once - Kalshi can't show it again.

## 3. Run

1. Double-click **start.bat**. Your browser opens to http://127.0.0.1:8765.
2. **Settings** -> paste the demo Key ID and the full path to `demo.key` -> Save.
3. The header shows `DEMO` and `API key ✓`.

When you're satisfied in demo, create a production key at kalshi.com/account/profile, save it
as `keys\prod.key`, enter it under `prod`, and switch Environment to Production. The header
turns red and every real-money order asks you to confirm twice.

## What's inside

| Tab | What it does |
|---|---|
| Rewards | Liquidity-reward quoter status, live quotes, rewards Kalshi has credited, P&L today vs your loss limit. |
| Scanner | Runs every 2 min over all open events. Signals with **Approve** can be executed in one click. |
| Markets | Search/browse every open event by category, live order book, rules, order ticket with fee + break-even, and **your fair value**. |
| Portfolio | Cash, positions with mark-to-bid, one-click close, resting orders (cancel / cancel all), fills, settlements with net P&L. |
| Ledger | Every order attempt the app made (placed or rejected and why) plus an audit log. |
| Settings | Keys, environment, risk limits, auto-trading, scanner. |

### Scanner strategies
- **basket_no** - On a mutually-exclusive event, buying NO on every outcome pays at least N-1 dollars. Flags when the total cost plus fees is below that. This is the only true arbitrage here. Its risk is *leg risk*: legs are sent fill-or-kill one at a time, and if a later leg misses, earlier legs stay filled. The ledger and audit flag `LEG_RISK`.
- **basket_yes** - Buying YES on every outcome pays $1 only if exactly one outcome wins. If the event can resolve with none of the listed outcomes, you lose the basket. **Read the rules.** This strategy can never auto-trade.
- **fair_value** - You enter your own probability for a market (your model, a sharp sportsbook's no-vig line, etc.). Flags when Kalshi's ask beats it by `min_edge` after fees. This is where your NFL/NBA framework plugs in.
- **wide_spread** - Liquid markets with spreads of 8c or more. A post-only quote inside the spread can earn it. This is informational and not an arbitrage.
- **movers** - Moves of 10c or more versus the prior price. Informational.

Expect **few or no signals most of the time**. Real edges on a liquid exchange are rare and close fast.

### Risk controls (Settings)
Every order (manual, approved, or automatic) is checked against these limits:
- $ per order
- $ of new buys per day
- $ per market
- $ total exposure
- contracts per order
- min/max buy price (minimum is 10c by default; contracts at 10c or less lose buyers over 60% on average)
- $ loss per day: if equity falls this much since the day's first check, the kill switch trips

Daily spend counts **filled** buys only. Unfilled and cancelled orders count $0. Blocked orders are recorded in the Ledger with the reason.

Two more guards apply:
- **Closing orders** are always allowed, so you can exit.
- **Sell YES / Sell NO** is blocked if you don't hold the position, so it can't accidentally open the opposite side.

**Kill switch** (red button) does three things at once:
- blocks all new orders
- turns auto-trading off
- cancels every resting order

### Liquidity quoter (v1.1)
Kalshi's Liquidity Incentive Program pays for resting orders near the top of the book, whether or not they fill. The program is listed through Jan 1, 2027, but Kalshi can end it at any time. When enabled, the quoter:
- picks the markets with the largest daily reward pools (minimum $5/day by default)
- joins the best YES bid and the best NO bid with post-only orders, so on most series it pays $0 in fees
- stops adding to a side once the net position reaches the cap (30 contracts)
- pulls quotes and cools down for 10 minutes if the price moves 4c or more in one cycle
- stops quoting 6 hours before expected settlement, so it doesn't quote in-game
- caps total committed capital at $100
- gives every quote a ~2-minute expiry, so nothing is left resting if the app or laptop stops

The real cost is adverse selection: fills tend to come right before the price moves against you. Judge it on the Rewards tab over weeks: rewards credited minus losses on fills. Quoter orders are bound by the auto limits ($ per order, $ of fills per day).

### Auto-trading
Off by default. When on, only the strategies you tick are executed. Each one is sized automatically to fit the tighter **auto** limits ($10/order, $30/day by default). `basket_yes`, `wide_spread` and `movers` are excluded permanently.

## Fees
Estimates use Kalshi's published schedule:
- taker = `0.07 x C x P x (1-P)`
- maker = `0.0175 x C x P x (1-P)` on series that charge maker fees, else 0

Both are rounded up to the cent. The real fee is shown on each fill.

## Notes / limits
- Runs only while `start.bat` is open, and only on `127.0.0.1`. Nothing is reachable from your network.
- Keys never leave your computer. The browser never sees the key file path.
- Data: `kalshi_desk.db` (SQLite) and `config.json` live in this folder. Back them up if you care about the ledger.
- The app does not cover perps (margin), RFQs, block trades, or combos (multivariate). Combos are excluded from browsing.
- Not tax or legal advice. Sports contracts' legal status varies by state and can change by court ruling.
