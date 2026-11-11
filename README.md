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

### Weather fair values (v2.1)
Daily high/low temperature markets settle on the NWS Daily Climate Report (CLI) for the station named in each
market's rules (e.g. CLIMDW = Chicago Midway), calendar day in local STANDARD time. The Weather tab:
- collects, for every station those markets use: NWS National Blend of Models station forecasts (NBS, every 6 h),
  ASOS observations and the climate reports, from the Iowa Environmental Mesonet (public, no key);
- prices every bucket: forecast extreme for the rest of the climate day + observed high/low so far + error
  distributions learned only from earlier days (incl. the usual gap between the climate report and the 5-minute obs);
- **Scorecard**: out-of-sample accuracy of the model vs the market's own price at 36/24/12/6/3/1 h before the day
  ends, plus a check that the climate report reproduces Kalshi's actual results;
- backtest strategy `weather_fair`: post-only bids `edge` below fair value on YES and NO.
Nothing here places orders. Kalshi's weather-index endpoints (`/live_data/weather/{city}`) are the multi-station
blend behind the *hourly* markets, not what the daily markets settle on, so v2.1 doesn't price from them.

### Market-making study (v2.3) - paper only
**Rewards** tab, top card. Nothing here places orders or touches your account; it uses production *public* data.
- **Scan reward markets**: every active liquidity-incentive program (all pages), each market's book, and Kalshi's
  published scoring (CFTC filing Jul 15 2026): bids walked from the best price down until Target Size; Reference
  Price where the size first reaches Target/5; score = size x DiscountFactor^ticks below it; each side normalised;
  snapshots without Target Size resting on *both* sides pay nothing; payouts under $1 per market program are zero.
  Shows best-case $/day by quote size and capital budget (before any fills).
- **Shadow quoter**: runs the live quoter's rules (join best YES/NO bid, 30-contract inventory cap, 4c jump pull +
  10-min cool-down, stop 6h before settlement, re-place every 60s) on the top markets from the scan, with virtual
  orders, polling the public book and trades (default 20 markets every 10s, ~2-3 requests/s). Reports rewards,
  fills under **queue** (back of the queue; conservative) and **touch** (optimistic) models, maker fees, inventory
  marked to the mid, net, and 1/10/60-minute markouts (adverse selection). It survives app restarts (open virtual
  quotes are dropped, as real ones would expire) and stops itself after 21 days.
- **Variant set** (v2.3.3): several paper runs side by side on one shared feed (identical data): *baseline* (live
  quoter settings), *quiet markets only* (<= 10 contracts traded in 24h), *1c behind the best bid* (fewer informed
  fills, but Kalshi's walk often reaches Target Size above you, which then pays nothing), and *pull after fills /
  bursts* (stop a side 10 min after it fills; pull both sides 10 min after 200+ contracts trade in one poll).
  Compared under both fill models; stop runs individually.
- The liquidity program is listed through **Jan 1, 2027**; Kalshi can extend, change or end it.
- The quoter's market picker now uses the same scoring (it used a simpler size ratio).
- Backtester: filled/expired orders are dropped from the matching loop (same results, much faster on long runs).

### Trading first, rewards as a bonus (v3.3)
- **Pass/fail** for every paper run is its **trading P&L alone** under strict fills; rewards are reported separately as a
  bonus. A run shows AHEAD/BEHIND until it has 5 days and 30 fills, then PASS/FAIL.
- **The quoter picks markets by estimated trading edge** (Settings -> *Pick markets by*; default): spread at least
  *Min spread* (2c) and `half the spread - maker fee - 0.25 x yesterday's price move` above zero. Ranked by that edge;
  estimated reward only breaks ties. A market needs no reward to qualify (the candidate list is still the ~6,500 markets
  that have a reward program). *Reward payout* restores the old ranking.
- The edge figure is a rough screen, not a forecast; the paper test (preset "trading-first picks") measures the real result.

### Limits that scale with the account (v3.2) - Settings
Tick *Scale limits with the account* and every dollar limit becomes a percentage of the account's value (cash +
positions), taken once each morning or when you press *Recalculate now*:

| Limit | under $250 | $250 - $1,000 | $1,000+ |
|---|---|---|---|
| Per order | 20% | 10% | 5% |
| New buys per day | 50% | 30% | 20% |
| Per market | 30% | 15% | 10% |
| Total exposure | 80% | 80% | 70% |
| Daily loss (kill switch) | 20% | 12% | 10% |
| Breaker: per strategy per day | 15% | 8% | 5% |
| Breaker: per strategy over 7 days | 30% | 16% | 12% |
| Breaker: account drop below peak | 30% | 20% | 15% |
| Quoter capital | 80% | 60% | 50% |

- A limit never drops when the account grows into a tighter tier: it holds at the previous tier's top value until the
  new percentage catches up (e.g. per order stays $50 from $250 to $500).
- The base is fixed for the day; a deposit takes effect the next morning or on *Recalculate now*.
- Also scaled: auto-trading limits (40% of the order limit, 30% of daily buys) and the quoter's markets (capital / $10,
  up to 5), contracts per quote (capital / markets / 2) and inventory cap (2 x contracts).
- Optional dollar ceilings per limit: `scaling.caps` in config.json (e.g. `{"order": 50}`). Tiers: `scaling.tiers`.
- While on, typed values for those dollar limits are ignored. Turn it off to go back to fixed limits.
- Deposits and withdrawals (Kalshi's transfer history) are netted out of the daily-loss limit and the drawdown breaker.

### Exit rules (v3.2) - Portfolio
Per position: *take* (sell when the bid for your side reaches this), *stop* (sell when it falls to this) and an optional
*floor* (never sell below this on a stop; you are alerted and it waits). Prices are in your side's terms. Checked every 30s;
sells the whole position at the bid, immediate-or-cancel, retrying what doesn't fill. A rule can only close a position, and
closing orders are allowed even with the kill switch on (as of v3.2 the kill switch blocks buys, not exits). No bid = nothing
is sent. A stop sells at whatever the bid is; in thin markets use the floor.

### Hardening (v3.1)
- **Quoter exits.** The side that reduces a position is sent as a sell of what you hold (sized to the position), so it is
  never blocked by the capital cap or the buy limits. At the inventory cap it steps 1c inside the spread when the spread is
  2c or wider (*exit_step_in*, on by default). Without this, a full cap stopped the quoter until settlement.
- **Stray orders.** If Kalshi's reply to an order is lost, the order is logged as `unknown` with its client id and audited.
  Every minute (and when it is switched off) the quoter cancels any resting order it sent but is not tracking - matched by
  client id, so manual orders are never touched. Also cleans up after a crash.
- **Resting orders count** toward the per-market and total exposure limits (worst case: they all fill). The quoter is
  governed by its own capital cap instead.
- **Settings file** is written atomically with a `.bak` of the last good copy; a damaged file is restored from it (or safe
  defaults) and you are alerted.

### Circuit breakers (v3.0) - Settings
Every 5 minutes, for the current environment:
- **Per strategy**: P&L of that strategy's own fills (today and last 7 days), each fill marked to the market (settled
  result, else the mid). Liquidity rewards are not counted (conservative). If a loss reaches *max $ loss today* or
  *max $ loss over 7 days*, that strategy is turned off (quoter disabled + quotes cancelled; auto strategy removed from
  auto-trading), you get a phone alert, and it can't be switched back on until you press *Reset*. Manual and outside
  fills are shown but never trip anything.
- **Account**: equity (Kalshi's cash + positions) vs its highest value in the last 30 days. A drop of *max $ below peak*
  trips the kill switch. Withdrawals look like a drop: press *Reset peak* after withdrawing.
- All limits are 0 (off) until you set them.

### Tax export (v3.0) - Ledger
*Download CSV* for a year: every fill, settlement and credit (liquidity rewards etc.) from Kalshi's records for the current
environment, with cash flow (buys negative, sells/settlements/credits positive), fees, and a total row (net = cash flow -
fees; positions still open at year end are at cost). A worksheet for your accountant, not tax advice; check it against
Kalshi's own year-end statements.

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
- blocks all new buys (sells that close a position you hold stay allowed, so you can always exit)
- turns auto-trading off
- cancels every resting order

### Liquidity quoter (v1.1)
Kalshi's Liquidity Incentive Program pays for resting orders near the top of the book, whether or not they fill. The program is listed through Jan 1, 2027, but Kalshi can end it at any time. When enabled, the quoter:
- scores every market with a liquidity program using Kalshi's formula at your quote size and picks the best estimated reward per $ of capital, until *Markets at once* or *Max $ committed* (re-picked every 10 minutes; pools under *Min pool $/day* ignored)
- joins the best YES bid and the best NO bid with post-only orders, so on most series it pays $0 in fees
- stops adding to a side once the net position reaches the cap (30 contracts)
- pulls quotes and cools down for 10 minutes if the price moves 4c or more in one cycle
- stops quoting 6 hours before expected settlement, so it doesn't quote in-game
- caps total committed capital at $100
- gives every quote an expiry (*Quote lifetime*, default 10 minutes) so nothing is left resting long if the app or laptop stops; a quote is only re-placed shortly before it expires, so it keeps its place in the queue

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
