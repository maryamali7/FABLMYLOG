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
| `live` | `.env` keys + `BOT_MODE=live` | Signed REST orders (Binance market) — **real money** |

Copy `.env.example` to `.env` only if you intend to go live. Never commit secrets.

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
              Strategy ensemble  →  RiskGate  →  Paper (or live) execution
                    │
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

Keep the process under systemd, Docker, or any supervisor. Websocket clients reconnect with backoff; the loop never sleeps the risk manager.

If the host cannot complete TLS to the venues (some locked-down sandboxes), FablMyLog automatically runs a **paper simulated tape** so the robot, risk engine, and dashboard stay alive. The moment Binance/Bybit/OKX/Coinbase/Kraken are reachable, live sockets take priority over the simulator.

## Screeners & advanced book

The terminal now includes:

- **14 screener boards** — alpha score, gainers, losers, volume spike, RSI OS/OB, squeeze, breakout, ADX trend, vs BTC, z-score mean-reversion, ATR vol, hot money, dump-bounce
- **18 strategies** — Supertrend, Ichimoku, Stoch RSI, VWAP, ADX, Keltner, Donchian, z-score, volume climax, ROC, CCI, ATR channel plus the originals
- **ATR stops**, **50% scale-out at 0.5R**, **BTC regime** (risk-on / risk-off size cut), **Kelly-ish sizing** after 8 trades
- Click a strategy card to arm/disarm it live; **watch** a screener hit onto the sockets

## Tests

```bash
pip install pytest
pytest -q
```
