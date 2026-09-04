from __future__ import annotations

import logging
from typing import Any

import httpx

from app.models import Candle
from app.symbols import compact, from_binance

log = logging.getLogger("rest")

BINANCE = "https://api.binance.com"


async def fetch_klines(symbol: str, interval: str = "1m", limit: int = 200) -> list[Candle]:
    pair = compact(symbol)
    url = f"{BINANCE}/api/v3/klines"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, params={"symbol": pair, "interval": interval, "limit": limit})
        r.raise_for_status()
        rows = r.json()
    out: list[Candle] = []
    for row in rows:
        out.append(
            Candle(
                ts=row[0] / 1000,
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
        )
    return out


async def fetch_universe(quote: str = "USDT", limit: int = 250) -> list[dict[str, Any]]:
    """All listed crypto pairs on Binance, ranked by 24h quote volume."""
    async with httpx.AsyncClient(timeout=20) as client:
        info = await client.get(f"{BINANCE}/api/v3/exchangeInfo")
        info.raise_for_status()
        tickers = await client.get(f"{BINANCE}/api/v3/ticker/24hr")
        tickers.raise_for_status()
    tradable = set()
    for s in info.json().get("symbols", []):
        if s.get("status") == "TRADING" and s.get("quoteAsset") == quote and s.get("isSpotTradingAllowed"):
            tradable.add(s["symbol"])
    ranked: list[dict[str, Any]] = []
    for t in tickers.json():
        sym = t.get("symbol")
        if sym not in tradable:
            continue
        ranked.append(
            {
                "symbol": from_binance(sym),
                "last": float(t.get("lastPrice") or 0),
                "change_pct": float(t.get("priceChangePercent") or 0),
                "volume": float(t.get("quoteVolume") or 0),
                "high": float(t.get("highPrice") or 0),
                "low": float(t.get("lowPrice") or 0),
            }
        )
    ranked.sort(key=lambda x: x["volume"], reverse=True)
    return ranked[:limit]


async def ping_exchanges() -> dict[str, bool]:
    urls = {
        "binance": "https://api.binance.com/api/v3/ping",
        "bybit": "https://api.bybit.com/v5/market/time",
        "okx": "https://www.okx.com/api/v5/public/time",
        "coinbase": "https://api.exchange.coinbase.com/time",
        "kraken": "https://api.kraken.com/0/public/Time",
    }
    out: dict[str, bool] = {}
    async with httpx.AsyncClient(timeout=8) as client:
        for name, url in urls.items():
            try:
                r = await client.get(url)
                out[name] = r.status_code < 500
            except Exception:
                out[name] = False
    return out
