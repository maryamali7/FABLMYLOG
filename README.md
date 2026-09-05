# FablMyLog — 24/7 multi-exchange crypto robot

A full trading terminal that:

- Streams **live public websockets** from Binance, Bybit, OKX, Coinbase, and Kraken
- Loads the **entire USDT spot universe** (hundreds of coins) and lets you socket-watch any of them
- Runs an **ensemble** of RSI, MACD, EMA trend, Bollinger, breakout (optional grid)
- Enforces **risk**: position caps, daily loss, max drawdown, spread filter, stops, trailing stops, cooldowns
- Defaults to **paper trading on real prices** so it can run 24/7 without keys
- Optionally routes **live Binance orders** if `BOT_MODE=live` and API keys are present
- Lets you build **custom strategies visually** — indicator rules, no code — and trades them live
- **Backtests** any strategy (custom or built-in) over real candles in milliseconds
- Ships an **advanced screener**: 40+ factors, custom queries, presets, grades and CSV export
- Runs **multi-timeframe analysis** — RSI, trend, ADX, MACD and a rating on 1m/5m/15m/1h/4h/1d/1w with an alignment verdict
- **Predicts the next move** with a six-model ensemble: direction, probability, expected move, target, cone, S/R and a plain-English "why"
- Runs a real **trade desk**: market/limit/stop/stop-limit/trailing orders, IOC/FOK/DAY, reduce-only,
  post-only, OCO brackets, a click-to-trade depth ladder and four sizing modes
- Plots **volume dots** — a TradingView-style chart of the consolidated tape from every connected
  exchange, one dot per bar sized by volume and coloured by buy/sell delta, with cumulative delta
  and an order-flow read of the next move
- Measures **portfolio risk**: exposure, concentration, correlation matrix, open risk, historical VaR
  and the R distribution of every closed trade
- Fires **custom alert rules** (with cooldowns, auto-watch and webhooks)
- Reports **performance analytics** per strategy, symbol, exit reason and hour
- Serves a dark trading dashboard with candles, order book, print tape, arb scanner, fills, and journal

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app
```

Open the dashboard at `http://localhost:8000`.

The robot boots, seeds 1m candles, connects every enabled exchange websocket, and starts the paper engine immediately.

## Paper vs live

| Mode | How | What happens |
|------|-----|----------------|
| `paper` (default) | no keys needed | Fills at live bid/ask plus configured fees & slippage |
| `live` | keys in the dashboard (or `.env`), connection test passed, orders enabled for the venue, then type `ARM LIVE` | Signed market orders on Binance/Bybit spot, capped per entry — **real money** |

Keys entered in the dashboard are encrypted at rest under `data/` and never
returned to the browser; `.env` still works if you prefer. Never commit secrets.
See **Bot control** below for the full arming sequence and the 24/7 supervisor.

Crypto trading can lose 100% of capital. This is software, not financial advice.

## Architecture

```
exchanges WS ─┐
              ├─ MarketHub (normalized tickers / books / trades / candles)
Binance REST ─┘
                    │
        ┌───────────┴────────────┐
        ▼                        ▼
  MTFEngine (1m…1w)        Rule frames / screener
        │                        │
  Universe index ── 8 venues · spot / linear perps / coin-margined (22 catalogs)
        │                        │
        └────────► Forecast ensemble (6 models)
                    │
                    ▼
         Strategy ensemble → TradeSet (coin picker) → Edge gate (quality,
              sizing, exits) → RiskGate → Paper or live execution
                    │                              ▲
   Trade desk ──► OMS (limit/stop/trail, OCO, TIF) ─┤
                    │                   Supervisor (24/7 watchdog)
                    │
                    ├──► Portfolio risk (exposure, correlation, VaR, R)
                    ├──► TapeBook (per-venue prints) → volume dots + flow read
                    ▼
              SQLite journal + FastAPI / WebSocket dashboard
```

## Custom strategy builder

Open **Strategy builder** in the dashboard. A strategy is a set of conditions over
indicator fields — pick a field, a comparator and a value (or another field):

```jsonc
{
  "name": "Squeeze breakout",
  "side": "long",
  "confidence": 0.72,
  "stop_loss_pct": 0.018,
  "take_profit_pct": 0.045,
  "entry": {
    "op": "all",                                   // all | any | none
    "rules": [
      { "left": "squeeze",   "cmp": "is_true" },
      { "left": "close",     "cmp": ">=",      "right": "hh20" },
      { "left": "vol_ratio", "cmp": ">",       "right": 1.6 },
      { "left": "ema9",      "cmp": "cross_above", "right": "ema21" }
    ]
  },
  "exit": { "op": "any", "rules": [{ "left": "close", "cmp": "cross_below", "right": "ema21" }] }
}
```

* **90+ fields** — price/structure, EMAs, RSI/stoch/MACD/CCI/Williams, ATR, Bollinger,
  Keltner, squeeze, Donchian, ADX/DI, supertrend, Ichimoku, VWAP, OBV, volume z-score,
  plus composite trend/momentum scores. `GET /api/builder/catalog` returns the full list.
* **15 comparators** — `> >= < <= == != between outside cross_above cross_below rising
  falling is_true is_false contains`.
* **Free-form expressions** too: `{"expr": "rsi < 30 and close > ema50 * 0.98"}`, parsed by a
  whitelisted AST evaluator (no `eval`, no imports, no attribute access).
* **9 starter templates**, per-strategy weight, confidence, cooldown, symbol scope and its own
  stop / target / trailing stop, which survive the ensemble vote.
* **Validate & preview** dry-runs the rules across the watchlist and shows which symbols match
  right now, with a per-condition trace (`rsi(28.4) < 30 → True`).

Saved strategies persist to `data/custom_strategies.json` and trade inside the same ensemble,
risk gate and journal as the built-ins.

## Backtest lab

`POST /api/backtest` (or the panel next to the builder) replays a strategy bar by bar with
fees, slippage, stops, targets, trailing stops and a time stop:

| Output | |
|---|---|
| Metrics | return %, buy & hold, trades, win rate, profit factor, expectancy, payoff, max drawdown, Sharpe, Sortino, exposure, avg bars held, grade A+→D |
| Series | equity curve + trade list with entry/exit/reason |
| Modes | single symbol, **basket** (`symbols: [...]`) and **compare** against built-ins |

## Advanced screener

* **40+ factors per symbol** and composite **alpha**, **quality**, **risk** and **liquidity**
  scores plus an A–D grade and a 10-factor confluence count.
* **30 boards** — alpha, confluence, quality, gainers/losers, volume, RSI extremes, squeeze,
  coiled, breakouts, fresh MACD crosses, VWAP reclaims, ADX trend, relative strength,
  mean reversion, volatility, hot money, dump bounce, low-risk trend, short setups, liquidity,
  plus **MTF aligned long/short**, **timeframe conflict**, **overbought / oversold stacks**,
  **predicted up / down** and **high conviction**.
* **Custom queries** — `POST /api/screener/query` with any number of field conditions,
  `match: all|any|none`, sorting, search and limit.
* **12 saved presets** (multi-TF long stack, HTF trend + LTF dip, overbought on every frame,
  predicted movers, breakout ready, oversold reversal, clean trend, high confluence,
  low-risk alpha, volatility hunters, BTC outperformers, short pressure).
* **Breadth summary** and **CSV export** (`/api/screener/export.csv`).

## Multi-timeframe analysis

Every watched symbol is rebuilt on **seven timeframes — 1m, 5m, 15m, 1h, 4h, 1d, 1w**.
Candles come from exchange REST history when the venue is reachable and are otherwise
resampled from the live 1m rolling window, so the feature degrades instead of disappearing.

Per timeframe: RSI + state (overbought / bullish / neutral / bearish / oversold), stochastic,
StochRSI, MACD state, ADX with ±DI, EMA stack, supertrend, Ichimoku cloud, ATR%, Bollinger %B,
squeeze flag, a −100…+100 rating and a *strong buy → strong sell* label.

Those frames are blended into one **alignment verdict** — a weighted score (higher timeframes
count more), agreement %, bull/bear counts and explicit **conflicts** such as
`15m overbought` while the 1h still trends up.

| Route | Purpose |
|---|---|
| `GET /api/mtf/{symbol}` | every timeframe + alignment for one coin |
| `GET /api/mtf` | alignment table across the watchlist |
| `POST /api/mtf/refresh?symbol=` | force a rebuild |
| `GET /api/candles/{symbol}?interval=15m` | resampled candles for any timeframe |

## Next-move prediction

`POST`-free `GET /api/predict/{symbol}?tf=15m` runs an ensemble of six models and returns a
single actionable forecast:

| Model | Reads |
|---|---|
| Trend | ADX / ±DI, supertrend, EMA stack, slope |
| Mean reversion | z-score, RSI, Bollinger %B |
| Analog | k-NN over normalized 20-bar return shapes — "what happened last time it looked like this" |
| Drift regression | OLS fit of log price with R² as confidence |
| Order flow | OBV, volume z-score, buy/sell pressure, book imbalance |
| Volatility | EWMA (λ = 0.94) sizing of the expected range |

Output: direction, probability up/down, expected move %, target with an upper/lower band,
confidence, risk/reward to the nearest levels, a projected **cone path** for the chart,
clustered **support / resistance** levels, the market **regime** and a ranked `rationale[]`
explaining the call. Forecasts for the whole watchlist are cached by the robot loop and
ranked at `GET /api/forecasts`; `GET /api/levels/{symbol}` returns just the structure.

Multi-timeframe and forecast values are first-class **rule-engine fields**, so builder
strategies, screener queries and alert rules can all say things like
`trend_1h == up AND rsi_15m < 40 AND prob_up > 58`.

### Prediction scoreboard

Every forecast is logged with its horizon and **graded against the real price** once that
horizon elapses (`GET /api/forecasts/accuracy`):

* **hit rate** and edge vs a coin flip, **Brier score**, **band coverage** (did price finish
  inside the predicted range) and mean absolute error of the expected move,
* accuracy **per model** — so you can see whether the analog or the trend model is actually
  carrying the ensemble — and **per timeframe**,
* a **calibration curve**: when the ensemble says 70%, does it happen 70% of the time?

On boot the scoreboard is seeded from candle history: the ensemble is re-run on truncated
windows (it only ever sees bars that existed at that moment) and graded against the price
that actually printed one horizon later. `POST /api/forecasts/backfill` re-runs it.

### Timeframe rules in backtests

The backtester rebuilds the higher timeframes from the same candles and replays them
**bar by bar on closed bars only**, so a strategy using `trend_1h` or `rsi_15m` is tested
without look-ahead. Results list which frames were replayed, and warn when a rule uses a
live-only forecast field that cannot be simulated.

## Instrument universe (8 venues · spot · perps · coin-margined)

The **Universe** view indexes *every* instrument on eight venues across three market types,
from public, key-free endpoints — 22 catalogs pulled in parallel:

| Venue | Spot | Linear perps | Coin-margined |
|---|---|---|---|
| Binance | `api/v3/ticker/24hr` | `fapi` ticker + `premiumIndex` | `dapi` ticker + premium (perp & dated) |
| Bybit | `v5/market/tickers?category=spot` | `category=linear` | `category=inverse` |
| OKX | `instType=SPOT` | `instType=SWAP` (stable-margined) | `instType=SWAP` (USD-margined) |
| MEXC | `api/v3/ticker/24hr` | `contract/api/v1/contract/ticker` | — |
| KuCoin | `market/allTickers` | `contracts/active` (XBT→BTC) | `contracts/active` USD-quoted |
| Gate.io | `v4/spot/tickers` | `v4/futures/usdt/tickers` | `v4/futures/btc/tickers` |
| Bitget | `v2/spot/market/tickers` | `USDT-FUTURES` | `COIN-FUTURES` |
| HTX | `market/tickers` | `linear-swap` + batch funding | — |

Everything is normalized into one row (`venue`, `market`, `symbol`, `base`, `quote`, `last`,
`change_pct`, `volume_usd`, `funding_rate`, `open_interest`, `contract`, `source`) and indexed
in memory with a 15-minute TTL refresh. Dated contracts keep their own id, so a September
future never overwrites the perp.

**Browsing**

- venue chips (8), market chips (spot / linear perps / coin-margined), quote, free text;
- numeric filters: min & max volume, 24h change band, funding band;
- sorts: volume (high/low), gainers, losers, funding (high/low), open interest, price, symbol, venue;
- **10 one-click presets** — volume leaders, gainers, losers, perps only, coin-margined,
  funding squeeze, shorts pay, open-interest leaders, illiquid, USDC books;
- **per-coin mode** merges every listing of an asset: how many venues list it, spot vs perp
  coverage, aggregate volume, cross-venue spread, average funding;
- click any symbol for a **coin drawer** with every listing side by side (price, 24h, volume,
  funding, OI, contract type) plus the venue spread and a one-click watch;
- CSV export of the current filter, and watch buttons that push instruments into the live
  watchlist (capped by `max_watch_symbols`).

**Cross-venue boards**

- **Spread** — same coin, cheapest venue vs richest, spot or perps.
- **Funding & basis** — who pays funding, funding APR, perp premium/discount to spot.
- **Cash-and-carry** — buy spot on the cheapest venue, short the best-paying perp;
  `carry_apr = funding APR + basis`, before fees, borrow and slippage.
- **24h movers** — best and worst performers across all venues, deduped per coin.
- **Venue exclusives** — coins listed on exactly one venue (listing alpha, and listing risk).

If every venue is unreachable (air-gapped host, blocked TLS, exchange outage) the index falls
back to a bundled offline catalog — ~4,000 instruments over 310 coins with **simulated prices**.
Those rows are tagged `source: "offline"` and the UI shows a red banner saying so; simulated
data is never presented as market data. Partial success is never overwritten: if twenty
catalogs load and two fail, you get the twenty plus the two listed failures.

## Bot control: what it trades, how well, and around the clock

### Pick the coins it trades

Watching and trading are separate. The **Bot control** view lists every coin on
the watchlist with a checkbox — tick one, tick ten, or switch mode:

| Mode | Behaviour |
|---|---|
| `selected` | trade exactly the coins you ticked (default; nothing trades until you pick) |
| `all` | trade anything on the watchlist |
| `auto` | trade the top N coins by score / alpha / momentum / volume, refreshed as the board moves, with optional pinned coins and a volume floor |

Per-coin you can also set a **size multiplier** (0.1×–3×) or disarm a single coin
without retyping the list. Selection is persisted in `data/trading.json`, so a
restart resumes exactly where you left off. Positions already open are always
managed to their exit, even if you untick the coin.

### Edge engine — the win-rate and ROI layer

The strategy ensemble says *"this looks interesting"*. The edge engine decides
whether it is worth money:

**Entry gate** scores every candidate 0-100 across eight factors — signal
confidence, multi-timeframe agreement, forecast probability, market regime,
trend location, volatility band, spread/liquidity, and the *live* track record
of that strategy and that coin. Hard blocks reject outright: timeframes
disagreeing, bearish higher-timeframe bias, risk-off regime, RSI overextended,
ATR too dead or too wild, spread too wide, daily trade cap, consecutive-loss
stand-down, per-coin cooldown, too many correlated positions, out-of-session
hours, and any strategy whose live win rate has fallen below your floor.

Every rejection is stored with its reason and surfaced in a **"Why no trade?"**
panel — the bot tells you exactly what it is waiting for instead of sitting
silently.

**Sizing** is volatility-targeted: risk a fixed % of equity per trade based on
ATR rather than a fixed notional, then scale by quality score and a Kelly
fraction derived from live results (capped, and floored so a bad streak shrinks
size instead of stopping it dead).

**Exits** are managed on every tick: ATR stop, break-even move at your chosen R,
a two-step partial-profit ladder, ATR trailing once in profit, a giveback lock
(never let a 3R winner become a loser) and a time stop for trades that go
nowhere. All of it is tunable from the dashboard and persisted in
`data/edge.json`.

### Exchange API keys

Add keys per venue in the dashboard. They are **encrypted at rest** (Fernet;
set `FABL_SECRET_KEY` to control the key) in `data/api_keys.json` with `0600`
permissions, and the API only ever returns a masked fingerprint — the secret is
never sent back to the browser. `.env` variables still work as a fallback.

Going live is deliberately awkward, in this order:

1. save the key and secret (plus passphrase for OKX/KuCoin/Bitget),
2. run **Test** — a signed, read-only balance call,
3. flip **orders** on for that venue,
4. type `ARM LIVE`, choose the venue and a per-order notional cap.

Order routing is implemented for **Binance and Bybit spot**. Other venues store
credentials and can be connection-tested, but the router refuses to invent a
signing scheme it cannot verify. Live orders mirror the paper engine: the same
entry gate, sizing and exits, capped by `max_notional` per entry, with routing
errors surfaced instead of swallowed.

### Running 24/7

A supervisor task runs beside the bot:

- **stall watchdog** — restarts the trading loop if it stops ticking for
  `stall_timeout_sec`,
- **daily roll** — resets the loss budget and trade counter at a chosen UTC hour,
- **auto-resume** — optionally clears a risk halt after a cooling-off period,
- **maintenance window** — pause (and optionally flatten) during a UTC hour range,
- **uptime accounting** — uptime, loop age, restart count and an event log.

For real 24/7 you still want the process supervised by the OS:

```ini
# /etc/systemd/system/fablmylog.service
[Unit]
Description=FablMyLog trading terminal
After=network-online.target

[Service]
WorkingDirectory=/opt/FABLMYLOG
ExecStart=/opt/FABLMYLOG/.venv/bin/python -m app
Environment=FABL_SECRET_KEY=change-me
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now fablmylog     # or: docker run --restart=unless-stopped ...
```

## Trade desk

A proper order-management system sits between you and the paper engine, so a manual trade behaves
the way it would on an exchange rather than being an instant market fill.

**Order types** — `market`, `limit`, `stop`, `stop_limit`, `trailing_stop`.
**Time in force** — `gtc`, `ioc`, `fok`, `day` (expires at the next UTC midnight).
**Flags** — `reduce_only` (never flips you short, sized down to the position), `post_only` (rejected
at placement if it would cross the spread).

Matching rules the engine applies on every tick:

| Order | Fills when | At |
|---|---|---|
| limit buy | `ask <= price` | `min(price, ask)` |
| limit sell | `bid >= price` | `max(price, bid)` |
| stop buy | `last >= stop` | the offer, after triggering |
| stop sell | `last <= stop` | the bid, after triggering |
| stop-limit | trigger first, then rests as a limit | limit rules |
| trailing stop | `peak` ratchets up with price, `stop = peak × (1 − trail)` | the bid |

### Sizing

The ticket takes quantity four ways, and the preview always shows notional, % of equity, fee, the
cash loss if the stop is hit, and reward:risk before you commit:

| Mode | Field | Meaning |
|---|---|---|
| Base | `qty` | coins |
| Quote | `quote_qty` | dollars of notional |
| % equity | `equity_pct` | share of mark equity |
| Risk % | `risk_pct` + a stop | `qty = equity × risk% / (entry − stop)` |

Risk-based sizing is the one worth using: it holds the *loss* constant instead of the *position*, so a
tight stop buys a big position and a wide stop buys a small one. The ticket refuses to size when the
stop is on the wrong side of the entry, and warns when the result would break your position cap.

### Brackets and OCO

Tick **attach bracket** and the stop-loss and take-profit are placed with the entry as one OCO group:
whichever fills first cancels its sibling. Bracket children sleep until their parent entry fills (so
the reduce-only sweep cannot cancel them before the position exists), and they are cancelled with the
entry if you pull it. Closing a position sweeps every remaining order on that symbol.

Desk positions carry a `manual` tag: the signal engine **will not close them** on an opposing
ensemble signal unless you hand them over with the *robot may close desk positions* switch. Stops,
targets and the edge exit manager always still apply.

### Depth ladder and time & sales

Ten levels each side with size bars; click any price to load it into the ticket (and flip a market
order to a limit). Below it, the last fifteen closed trades with PnL and R.

### Command palette

`Ctrl`/`⌘`+`K` anywhere: jump between views, buy 1% of equity, close or flatten the current symbol,
cancel one symbol's orders or the whole book, pause/resume the robot, or jump the desk to any
watchlist coin.

## Volume dots

A candle tells you where price went. It does not tell you who moved it, whether the move was paid
for, or whether the other side quietly took the whole thing. **Delta** — aggressive buying minus
aggressive selling — does, and this view is built entirely on it.

The chart is [lightweight-charts](https://github.com/tradingview/lightweight-charts) v5, **vendored
into `web/vendor/`** rather than pulled from a CDN, so the dashboard still works on an air-gapped box.
Three panes: candles with the dots on top, a delta histogram, and the cumulative delta line.

### What a dot is

Every dot is one bar of the **consolidated cross-exchange tape**:

| Property | Meaning |
|---|---|
| position | the bar's volume-weighted price |
| colour | green when buyers were the aggressors, red when sellers were |
| size | total volume, graded against the rest of the window (1-5) |
| opacity | how one-sided the flow was |
| gold square | the bar disagrees with its own candle — absorption or divergence |

The interesting dots are the ones that disagree with their candle. A red dot on a green bar means
sellers hit every bid and price went **up** anyway: someone with size absorbed them. That is the
signature that leads reversals, and it is what the read is looking for.

Each bar is classified: `absorption` (heavy one-sided flow, no candle), `divergence` (aggressors on
one side, price on the other), `initiative` (aggressors paid up and price followed), `pressure`,
`thin` (price moved on almost no net flow — easy to reverse) and `balanced`.

### The consolidated tape

`app/tape.py` records **every print, per symbol, per venue**, bucketed into 5-second cells as it
arrives so memory stays flat no matter how busy the tape gets — six hours of history per symbol.
Any chart timeframe (5s to 1h) is a resample of those cells, so switching timeframes is free.

Because the tape only starts when the robot does, older bars are estimated from candle shape — where
the close sits in the bar's range is a decent proxy for who won it. Those bars are **flagged
`estimated`** and the header says so; the read repeats the warning. Sub-minute timeframes never use
the estimate, because you cannot fake a 15-second bar from a 1-minute candle.

### The next-move read

Eight weighted signals combine into a score from −100 to +100:

1. **Delta tilt** — what share of the window's volume was aggressive, and on which side
2. **Price vs cumulative-delta divergence** — the single most useful thing on the panel: price
   grinding higher while CVD grinds lower means the rally is not being paid for
3. **Absorption clusters** — repeated bars where one side kept hitting and price would not move
4. **Initiative bars** — aggressors paid the spread and price followed
5. **Cross-exchange agreement** — every venue net-buying is very different from one venue buying
   while the rest sell; the biggest book gets the casting vote
6. **Size prints** — the notional skew of outsized trades
7. **Volume point of control** — whether price is accepted above or below where the volume traded
8. **Climax exhaustion** — the biggest delta of the window producing almost no candle

Every reason is shown with its weight and a plain-English explanation, so the score is auditable
rather than a black box. The **By exchange** table breaks the same window down per venue, and the
dropdown re-renders the entire chart from a single exchange's flow.

## Portfolio risk

`GET /api/portfolio` and the **Risk & portfolio** view answer the questions that decide whether a
book survives:

- **Exposure** — notional and weight per position, gross vs net, cash %, and a Herfindahl
  concentration score labelled *diversified / concentrated / single-name risk*
- **Open risk** — what it costs if every stop is hit at once, in dollars and % of equity, plus the
  open R on each trade and a list of positions with **no stop at all**
- **Correlation** — pairwise correlation of recent returns across the book, drawn as a heat map
  (red = moving together = one bet, not several) with the tightest pairs called out
- **Value at risk** — historical VaR 95/99 from the realised equity curve, expected shortfall
  (the average of the losses beyond VaR), volatility and the worst observed period
- **R distribution** — the histogram of closed trades in R multiples, expectancy, average win and
  loss, and the share of gross profit produced by the best decile of trades

The summary emits plain-English warnings: a name over 25% of equity, a concentrated book, unstopped
positions, open risk above 6%, or an average correlation over 0.7.

## Trade review

Every closed trade lands in the journal with its PnL, R multiple and exit reason. Tag it
(`breakout`, `fomo`, `news`), rate it 0-5 and write a note; the **Playbook tags** table on the risk
view then aggregates trades, win rate, net and average PnL per tag, so you can see which setups
actually pay and which ones only feel good.

## Alert rules & analytics

Alert rules reuse the same rule engine against screener rows, with severity, message
templating (`{symbol} {price} {alpha}`), per-symbol cooldowns, optional **auto-watch** and
**webhook** delivery. Analytics turns the fill journal into per-strategy, per-symbol,
per-exit-reason and hourly edge tables, streaks, a PnL histogram and equity-curve risk stats
(Sharpe, Sortino, Calmar, drawdown).

## API cheat sheet

| Method | Route | Purpose |
|---|---|---|
| GET | `/api/builder/catalog` | fields, comparators, templates, presets |
| GET/POST | `/api/strategies/custom` | list / create / update a builder strategy |
| POST | `/api/strategies/custom/validate` | validate + live preview of matches |
| POST | `/api/strategies/custom/{id}/toggle` · `/duplicate` | arm, disarm, clone |
| DELETE | `/api/strategies/custom/{id}` | remove |
| POST | `/api/backtest` | single, basket or comparison backtest |
| POST | `/api/screener/query` | filtered / sorted screen |
| GET | `/api/screener/presets` · `/export.csv` · `/symbol/{sym}` | presets, CSV, one row |
| GET/POST/DELETE | `/api/alerts/rules` | alert rule CRUD |
| GET | `/api/alerts/history` | triggered alerts |
| GET | `/api/analytics` | performance tables |
| GET/POST | `/api/risk` · `/api/risk/resume` | live risk tuning, clear a halt |
| GET | `/api/mtf` · `/api/mtf/{sym}` · `POST /api/mtf/refresh` | multi-timeframe alignment |
| GET | `/api/predict/{sym}?tf=&horizon=` | next-move forecast |
| GET | `/api/forecasts` · `/api/levels/{sym}` | ranked forecasts, support/resistance |
| GET | `/api/forecasts/accuracy` | hit rate, Brier, calibration, per-model scoring |
| POST | `/api/forecasts/backfill` | re-seed the scoreboard from candle history |
| GET | `/api/instruments` | every venue/market listing, filtered + sorted + paged |
| GET | `/api/instruments/stats` | per-venue counts, coins, volume, catalog source |
| GET | `/api/instruments/coins` | one row per coin, merged across venues |
| GET | `/api/instruments/arb` | widest cross-venue price gaps |
| GET | `/api/instruments/funding` | funding extremes + perp-vs-spot basis |
| GET | `/api/instruments/symbol/{sym}` | every listing of one symbol |
| GET | `/api/instruments/export.csv` | CSV of the current filter |
| POST | `/api/instruments/refresh` | re-pull catalogs (optionally one venue/market) |
| POST | `/api/instruments/watch` | add instruments to the live watchlist |
| GET | `/api/instruments/presets` | preset screens, venue and market lists |
| GET | `/api/instruments/carry` | cash-and-carry ranking (funding APR + basis) |
| GET | `/api/instruments/movers` | 24h gainers and losers across all venues |
| GET | `/api/instruments/exclusives` | coins listed on a single venue |
| GET/POST | `/api/trading/selection` | which coins the bot may trade |
| POST | `/api/trading/toggle` · `/select_all` | arm one coin, set its size ×, arm/clear all |
| GET/POST | `/api/edge` · `/api/edge/reset` | edge-engine stats and tuning |
| GET | `/api/edge/rejections` | why signals were not taken |
| POST | `/api/edge/preview` | score a symbol as if a signal fired |
| GET/POST/DELETE | `/api/keys` · `/api/keys/{venue}` | encrypted credential store |
| POST | `/api/keys/test` · `/api/keys/trading` | signed read-only test, enable orders |
| GET | `/api/live` · `POST /api/live/arm` · `/disarm` | live routing state and arming |
| GET/POST | `/api/uptime` · `POST /api/uptime/check` | 24/7 supervisor |
| GET | `/api/desk?symbol=` | quote, 12-level book, position, working orders, equity and last trades |
| POST | `/api/desk/settings` | hand desk positions to the robot, or take them back |
| GET/POST | `/api/orders` | working-order snapshot / place an order |
| POST | `/api/orders/{id}/cancel` · `/modify` | pull or amend a resting order |
| POST | `/api/orders/cancel_all?symbol=&side=` | sweep the book |
| POST | `/api/orders/bracket` | attach a stop / target / trail to an open position |
| GET | `/api/portfolio` | exposure, concentration, correlation, open risk, VaR, R distribution |
| GET/POST | `/api/journal` | closed trades with annotations / tag, rate and note one |
| GET | `/api/flow/{sym}?tf=&bars=&venue=` | volume dots, cumulative delta, per-venue split, flow read |
| GET | `/api/tape/stats` | consolidated tape coverage: symbols, cells, prints, venues |

## Config

`config.yaml` controls watchlist, strategy weights, and risk. Environment overrides:

- `BOT_MODE=paper|live`
- `STARTING_EQUITY=10000`
- `BOT_HOST` / `BOT_PORT`

## 24/7 deploy

```bash
docker build -t fablmylog .
docker run --restart=always -p 8000:8000 --env-file .env fablmylog
```

Keep the process under systemd, Docker, or any supervisor — there is a systemd
unit in **Bot control** above, and the in-process supervisor (stall watchdog,
daily roll, auto-resume, maintenance window) handles everything short of the
process dying. Websocket clients reconnect with backoff; the loop never sleeps
the risk manager.

If the host cannot complete TLS to the venues (some locked-down sandboxes), FablMyLog automatically runs a **paper simulated tape** so the robot, risk engine, and dashboard stay alive. The moment Binance/Bybit/OKX/Coinbase/Kraken are reachable, live sockets take priority over the simulator.

## Screeners & advanced book

The terminal now includes:

- **14 screener boards** — alpha score, gainers, losers, volume spike, RSI OS/OB, squeeze, breakout, ADX trend, vs BTC, z-score mean-reversion, ATR vol, hot money, dump-bounce
- **18 strategies** — Supertrend, Ichimoku, Stoch RSI, VWAP, ADX, Keltner, Donchian, z-score, volume climax, ROC, CCI, ATR channel plus the originals
- **ATR stops**, **50% scale-out at 0.5R**, **BTC regime** (risk-on / risk-off size cut), **Kelly-ish sizing** after 8 trades
- Click a strategy card to arm/disarm it live; **watch** a screener hit onto the sockets

## Third-party assets

`web/vendor/lightweight-charts.js` is TradingView's
[lightweight-charts](https://github.com/tradingview/lightweight-charts) v5.2.1 standalone build,
vendored so nothing is fetched from a CDN at runtime. Its Apache-2.0 licence is alongside it in
`web/vendor/lightweight-charts.LICENSE`.

## Tests

```bash
pip install pytest
pytest -q
```

231 tests cover the indicator maths, rule frames, screener, builder specs, forecast scoring, the
instrument universe, coin selection, the edge engine, credential storage, the supervisor, the order
matcher (triggers, OCO, post-only, IOC, reduce-only, trailing ratchet, bracket arming), the portfolio
risk maths, and the tape and order-flow layer (cell bucketing, late prints, memory bounds,
resampling, the candle-shape estimator, dot classification and every branch of the read).
