# FablMyLog — 24/7 multi-exchange crypto robot

A full trading terminal that:

- Streams **live public websockets** from Binance, Bybit, OKX, Coinbase, and Kraken
- Loads the **entire USDT spot universe** (hundreds of coins) and lets you socket-watch any of them
- Runs an **ensemble** of RSI, MACD, EMA trend, Bollinger, breakout (optional grid)
- Enforces **risk**: position caps, daily loss, max drawdown, spread filter, stops, trailing stops, cooldowns
- Defaults to **paper trading on real prices** so it can run 24/7 without keys
- Optionally routes **live Binance orders** if `BOT_MODE=live` and API keys are present
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
