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
* **22 boards** — alpha, confluence, quality, gainers/losers, volume, RSI extremes, squeeze,
  coiled, breakouts, fresh MACD crosses, VWAP reclaims, ADX trend, relative strength,
  mean reversion, volatility, hot money, dump bounce, low-risk trend, short setups, liquidity.
* **Custom queries** — `POST /api/screener/query` with any number of field conditions,
  `match: all|any|none`, sorting, search and limit.
* **8 saved presets** (breakout ready, oversold reversal, clean trend, high confluence,
  low-risk alpha, volatility hunters, BTC outperformers, short pressure).
* **Breadth summary** and **CSV export** (`/api/screener/export.csv`).

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
