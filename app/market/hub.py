from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import Any

import httpx

from app.config import Settings
from app.indicators import RollingWindow
from app.market.feeds import (
    BinanceFeed,
    BybitFeed,
    CoinbaseFeed,
    KrakenFeed,
    OkxFeed,
    SimulatedFeed,
    WSClient,
)
from app.models import OrderBook, Ticker, TradeTick
from app.symbols import compact, from_binance

log = logging.getLogger("hub")


class MarketHub:
    """Fan-in of major-exchange public websockets into a normalized book."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.tickers: dict[str, dict[str, Ticker]] = defaultdict(dict)  # symbol -> exchange -> ticker
        self.best: dict[str, Ticker] = {}
        self.books: dict[str, OrderBook] = {}
        self.trades: deque[TradeTick] = deque(maxlen=400)
        self.candles: dict[str, RollingWindow] = defaultdict(lambda: RollingWindow(6200))
        self.universe: list[dict[str, Any]] = []
        self.feeds: list[WSClient] = []
        self._lock = asyncio.Lock()
        self.started_at = time.time()
        self.arb: list[dict[str, Any]] = []
        self._backup_task: asyncio.Task | None = None
        self._symbols: list[str] = []

    async def start(self, symbols: list[str]) -> None:
        await self.stop()
        self.feeds = []
        ex = self.settings.exchanges
        makers = [
            ("binance", BinanceFeed, ex.get("binance")),
            ("bybit", BybitFeed, ex.get("bybit")),
            ("okx", OkxFeed, ex.get("okx")),
            ("coinbase", CoinbaseFeed, ex.get("coinbase")),
            ("kraken", KrakenFeed, ex.get("kraken")),
        ]
        for name, cls, cfg in makers:
            if not cfg or not cfg.enabled:
                continue
            feed = cls(
                url=cfg.ws,
                symbols=symbols,
                on_ticker=self.on_ticker,
                on_trade=self.on_trade,
                on_book=self.on_book,
            )
            self.feeds.append(feed)
            await feed.start()
        self._symbols = list(symbols)
        live = await self._exchanges_reachable()
        if not live:
            log.warning("venue TLS unreachable — starting paper simulator so the robot can still run 24/7")
            sim = SimulatedFeed(
                url="",
                symbols=symbols,
                on_ticker=self.on_ticker,
                on_trade=self.on_trade,
                on_book=self.on_book,
            )
            self.feeds.append(sim)
            await sim.start()
        if self._backup_task:
            self._backup_task.cancel()
        self._backup_task = asyncio.create_task(self._rest_backup(), name="rest-backup")
        log.info("market hub started with %d feeds, %d symbols", len(self.feeds), len(symbols))

    async def _exchanges_reachable(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get("https://api.binance.com/api/v3/ping")
                return r.status_code == 200
        except Exception:
            return False

    async def stop(self) -> None:
        if self._backup_task:
            self._backup_task.cancel()
            try:
                await self._backup_task
            except (asyncio.CancelledError, Exception):
                pass
            self._backup_task = None
        for f in self.feeds:
            await f.stop()
        self.feeds = []

    async def _rest_backup(self) -> None:
        """If a websocket goes quiet, keep marks alive via Binance REST."""
        while True:
            await asyncio.sleep(8)
            stale = []
            now = time.time()
            for sym in self._symbols:
                t = self.best.get(sym)
                if t is None or now - t.ts > 12:
                    stale.append(sym)
            if not stale:
                continue
            try:
                async with httpx.AsyncClient(timeout=12) as client:
                    r = await client.get("https://api.binance.com/api/v3/ticker/24hr")
                    r.raise_for_status()
                    wanted = {compact(s) for s in stale}
                    for row in r.json():
                        if row.get("symbol") not in wanted:
                            continue
                        last = float(row.get("lastPrice") or 0)
                        bid = float(row.get("bidPrice") or last)
                        ask = float(row.get("askPrice") or last)
                        t = Ticker(
                            exchange="binance",
                            symbol=from_binance(row["symbol"]),
                            last=last,
                            bid=bid,
                            ask=ask,
                            volume=float(row.get("volume") or 0),
                            ts=now,
                            high=float(row.get("highPrice") or 0),
                            low=float(row.get("lowPrice") or 0),
                            change_pct=float(row.get("priceChangePercent") or 0),
                        )
                        await self.on_ticker(t)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("rest backup: %s", exc)

    async def on_ticker(self, t: Ticker) -> None:
        if not t.symbol or not t.last:
            return
        self.tickers[t.symbol][t.exchange] = t
        prev = self.best.get(t.symbol)
        # prefer tightest spread / most recent binance as primary
        ranked = list(self.tickers[t.symbol].values())
        ranked.sort(
            key=lambda x: (
                0 if x.exchange != "sim" else 1,
                x.spread_bps if x.spread_bps else 999,
                -x.ts,
            )
        )
        self.best[t.symbol] = ranked[0]
        win = self.candles[t.symbol]
        win.tick(t.last, t.ts)
        if prev is None or abs(t.last - (prev.last or 0)) >= 0:
            self._refresh_arb(t.symbol)

    async def on_trade(self, tr: TradeTick) -> None:
        if not tr.price:
            return
        self.trades.appendleft(tr)
        self.candles[tr.symbol].tick(tr.price, tr.ts, tr.qty)

    async def on_book(self, book: OrderBook) -> None:
        if not book.symbol:
            return
        self.books[f"{book.exchange}:{book.symbol}"] = book
        prev = self.books.get(book.symbol)
        if prev is None or book.exchange == "binance":
            self.books[book.symbol] = book

    def _refresh_arb(self, symbol: str) -> None:
        books = self.tickers.get(symbol) or {}
        if len(books) < 2:
            return
        bids = [(ex, t.bid or t.last) for ex, t in books.items() if (t.bid or t.last)]
        asks = [(ex, t.ask or t.last) for ex, t in books.items() if (t.ask or t.last)]
        if not bids or not asks:
            return
        best_bid = max(bids, key=lambda x: x[1])
        best_ask = min(asks, key=lambda x: x[1])
        if best_bid[0] == best_ask[0]:
            return
        edge_bps = (best_bid[1] - best_ask[1]) / best_ask[1] * 10_000
        if edge_bps > 3:
            self.arb = [a for a in self.arb if a["symbol"] != symbol][:40]
            self.arb.insert(
                0,
                {
                    "symbol": symbol,
                    "buy_ex": best_ask[0],
                    "buy": best_ask[1],
                    "sell_ex": best_bid[0],
                    "sell": best_bid[1],
                    "edge_bps": round(edge_bps, 2),
                    "ts": time.time(),
                },
            )
            self.arb = self.arb[:25]

    def quote(self, symbol: str) -> Ticker | None:
        return self.best.get(symbol)

    def health(self) -> list[dict[str, Any]]:
        return [f.snapshot() for f in self.feeds]

    def ticker_table(self) -> list[dict[str, Any]]:
        rows = []
        for sym, t in sorted(self.best.items()):
            venues = self.tickers.get(sym, {})
            rows.append(
                {
                    "symbol": sym,
                    "last": t.last,
                    "bid": t.bid,
                    "ask": t.ask,
                    "change_pct": t.change_pct,
                    "volume": t.volume,
                    "spread_bps": round(t.spread_bps, 2),
                    "exchange": t.exchange,
                    "venues": {k: v.last for k, v in venues.items()},
                    "ts": t.ts,
                }
            )
        return rows
