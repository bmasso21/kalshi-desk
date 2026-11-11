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

### Real-time feed (v1.2)
The app keeps one authenticated WebSocket connection to Kalshi (`wss://external-api-ws[.demo].kalshi.co/trade-api/ws/v2`):
- **Account (all markets):** order updates (`user_orders`), fills (`fill`), positions (`market_positions`).
- **Markets:** live order books (`orderbook_delta`) and tickers (`ticker`) for the markets that matter right now: the quoter's markets, markets you hold, and any market you open in the UI (kept for 5 minutes). Up to 250 at once.
- Deltas are applied in sequence order. A missed message (sequence gap) triggers a fresh snapshot.
- If the connection drops, the app reconnects automatically (1s, 2s, 4s … up to 30s) and re-reads resting orders and recent fills over REST, so nothing is missed. **While it's down, everything falls back to REST polling** exactly as in v1.1.
- The header shows `LIVE · n/m books` (green) or `POLLING` (amber). `/api/feed` has the details.
- Every fill is stored in the `fills` table with its strategy and source (used by the closing-line-value tracker).
- The quoter now wakes on book changes in its markets (at most every 2s) instead of only every 15s.

### Phone alerts (v1.3, Pushover)
Settings → **Phone alerts**: paste your Pushover **user key** and an **application API token** (create an application at pushover.net). They are stored in `config.json` on this computer and never shown in the browser again.

| Event | Priority |
|---|---|
| Fill (bursts within 10s are batched into one message) | normal |
| Kill switch tripped automatically (loss limit), leg risk on a basket | **emergency**: repeats until you acknowledge it |
| Kill switch pressed by you | high |
| Errors (scan, auto, quoter, API, fill sync…) | high, at most once per kind every 30 min, with a count of the ones skipped |
| Live feed down 5+ min / restored | high / normal |
| App started (normal) or restarted after an unclean stop (high) | normal / high |
| App unreachable 2+ min (sent by the watchdog) | **emergency** |
| Daily P&L summary (default 23:50) | low (no sound) |

**Watchdog.** A separate process checks the server every 30s. If it stays unreachable for 2 minutes you get an emergency alert; 3 minutes later the watchdog kills the hung server so the boot task's launcher restarts it, and tells you. Update restarts (~20s) don't alert. Log: `logs\watchdog.log`.

**Whole machine offline.** Nothing on this computer can report that it lost power or internet. For that, create a free check at healthchecks.io (period 5 min), turn on its Pushover integration, and paste its ping URL into *Heartbeat URL*; the app pings it every minute.

### Research: data warehouse + backtester (v1.4)
**Research** tab.

*Warehouse* (`warehouse.db`). Collects Kalshi **production public** market data - no key, no account access, no orders - because demo books are nearly empty. Trading stays on demo. In scope by default: the *Climate and Weather* category, last 120 days (change categories / series / lookback on the tab). Stores markets (times, result, settlement value, volume), series fee types, every public trade, hourly YES bid / YES ask / price candles, and top-of-book changes recorded live from the WebSocket feed. Kalshi moves older data to `/historical/*` endpoints after a cutoff (`GET /historical/cutoff`); the collector routes each request to the right tier and normalises the field names. Runs hourly in the background at 3 requests/second; completed markets are never re-downloaded.

*Backtester*. Replays each market hour by hour. At each hourly close the strategy sees only the past (the result is hidden until settlement).
- **Taker** orders fill at that close's ask (+ optional slippage) and pay the taker fee.
- **Maker** orders rest at their price and only fill from later real trades, limited to each trade's size: **conservative** = a trade printed *through* your price; **optimistic** = also trades *at* your price on your counterparty's side. Results are shown under both, because nobody knows the real queue position.
- Fees use each series' fee type, rounded up per fill. Settlement pays $1 per winning contract.
- Reports: net P&L, fees, ROI, P&L per contract, average **closing-line value** (fill vs last trade before close), win rate, max drawdown, daily Sharpe, equity curve, and a per-series table.

Built-in reference strategies: `favorite_late` (taker; favourite-longshot bias test) and `maker_join` (the quoter's logic without rewards = its adverse-selection cost). Phase 2 strategies plug into the same engine.

### Closing-line value (v1.5)
**CLV** tab. Every fill (yours, the app's, or ones placed on kalshi.com) is scored once its market closes:
- **Closing line** = the market's last traded YES price before it closed, in the same environment as the fill (demo fills vs demo prices). Fallbacks: Kalshi's historical tier for archived markets, then the market's last price.
- **CLV per contract** = how much better your price was than that line, in your side's terms (buy YES: line - price; buy NO / sell YES: price - line in YES terms). Direction comes from Kalshi's `book_side`.
- Shown overall and by strategy, category and series for 7/30/90/365 days, weighted by contracts, with "% of contracts that beat the close". The daily summary includes the 30-day CLV.
- The backtester uses the same definition. Phase 3.2 will use the 30-day CLV per strategy to cut budgets automatically.

### Remote access (v1.6, Tailscale + password)
The server still listens only on `127.0.0.1`. `tailscale serve --bg 8765` publishes it as `https://<machine>.<tailnet>.ts.net`, reachable **only by devices signed in to your Tailscale account** (phone, laptop), with a real HTTPS certificate.
- **On this computer** the app works without a password, as before.
- **Remotely** a password is always required (Settings -> Remote access; at least 12 characters; stored as a PBKDF2 hash). Remote access is refused until a password has been set *on this computer*.
- Optional **"Only these Tailscale accounts"** list: other accounts are refused before they even see the login page.
- Sessions last 30 days (HttpOnly, Secure, SameSite=Strict cookie). "Log out everywhere" and password changes end all sessions.
- 5 wrong passwords in 15 minutes lock remote logins for 15 minutes and send a phone alert; each new remote login sends a silent alert.
- Real-money orders still need the double confirmation; nothing about trading permissions changes.

### Updating a running install
Drop an empty file named `restart.request` next to `run.py`. The app cancels quoter orders, exits, and the boot task restarts it about 15 seconds later.

### Tests
`.venv\Scripts\python run_tests.py` runs everything against strict offline mocks of Kalshi's REST and WebSocket APIs (needs `pip install -r requirements-test.txt`).

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
- Remote access (v1.6): see below. Without it, nothing is reachable from your network.
- Keys never leave your computer. The browser never sees the key file path.
- Data: `kalshi_desk.db` (SQLite) and `config.json` live in this folder. Back them up if you care about the ledger.
- The app does not cover perps (margin), RFQs, block trades, or combos (multivariate). Combos are excluded from browsing.
- Not tax or legal advice. Sports contracts' legal status varies by state and can change by court ruling.
