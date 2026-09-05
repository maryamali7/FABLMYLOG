from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import random
import time
from typing import Any, Awaitable, Callable

import websockets

from app.models import BookLevel, OrderBook, Ticker, TradeTick
from app.symbols import (
    COINBASE_USD_MAP,
    KRAKEN_MAP,
    from_binance,
    from_coinbase,
    from_kraken,
    from_okx,
    to_binance,
    to_bybit,
    to_coinbase,
    to_kraken,
    to_okx,
)

log = logging.getLogger("feeds")

OnTicker = Callable[[Ticker], Awaitable[None] | None]
OnTrade = Callable[[TradeTick], Awaitable[None] | None]
#: historical candle backfill: symbol, ts, open, high, low, close, volume
OnBar = Callable[[str, float, float, float, float, float, float], Awaitable[None] | None]
OnBook = Callable[[OrderBook], Awaitable[None] | None]


async def _maybe(cb, *payload) -> None:
    if cb is None:
        return
    res = cb(*payload)
    if asyncio.iscoroutine(res):
        await res


class WSClient:
    name = "base"

    def __init__(
        self,
        url: str,
        symbols: list[str],
        on_ticker: OnTicker | None = None,
        on_trade: OnTrade | None = None,
        on_book: OnBook | None = None,
        on_bar: OnBar | None = None,
    ):
        self.url = url
        self.symbols = symbols
        self.on_ticker = on_ticker
        self.on_trade = on_trade
        self.on_book = on_book
        self.on_bar = on_bar
        self.connected = False
        self.last_msg = 0.0
        self.errors = 0
        self.messages = 0
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "connected": self.connected,
            "last_msg": self.last_msg,
            "errors": self.errors,
            "messages": self.messages,
            "stale": (time.time() - self.last_msg) > 15 if self.last_msg else True,
        }

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name=f"ws-{self.name}")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._session()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors += 1
                self.connected = False
                if self.errors <= 2 or self.errors % 15 == 0:
                    log.warning("%s websocket error (%d): %s", self.name, self.errors, exc or "tls/connect failed")
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 1.8)

    async def _session(self) -> None:
        raise NotImplementedError

    def _mark(self) -> None:
        self.last_msg = time.time()
        self.messages += 1
        self.connected = True


class BinanceFeed(WSClient):
    name = "binance"

    async def _session(self) -> None:
        streams = []
        for s in self.symbols:
            b = to_binance(s)
            streams.append(f"{b}@ticker")
            streams.append(f"{b}@trade")
            streams.append(f"{b}@depth5@100ms")
        url = "wss://stream.binance.com:9443/stream?streams=" + "/".join(streams)
        async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**23) as ws:
            self.connected = True
            log.info("binance connected (%d streams)", len(streams))
            async for raw in ws:
                if self._stop.is_set():
                    break
                self._mark()
                msg = json.loads(raw)
                data = msg.get("data") or msg
                stream = msg.get("stream", "")
                if "ticker" in stream or data.get("e") == "24hrTicker":
                    await self._ticker(data)
                elif "trade" in stream or data.get("e") == "trade":
                    await self._trade(data)
                elif "depth" in stream:
                    await self._book(data, stream)

    async def _ticker(self, d: dict) -> None:
        sym = from_binance(d.get("s", ""))
        last = float(d.get("c") or 0)
        bid = float(d.get("b") or last)
        ask = float(d.get("a") or last)
        t = Ticker(
            exchange="binance",
            symbol=sym,
            last=last,
            bid=bid,
            ask=ask,
            volume=float(d.get("v") or 0),
            ts=float(d.get("E") or time.time() * 1000) / 1000,
            high=float(d.get("h") or 0),
            low=float(d.get("l") or 0),
            change_pct=float(d.get("P") or 0),
        )
        await _maybe(self.on_ticker, t)

    async def _trade(self, d: dict) -> None:
        tick = TradeTick(
            exchange="binance",
            symbol=from_binance(d.get("s", "")),
            price=float(d.get("p") or 0),
            qty=float(d.get("q") or 0),
            side="sell" if d.get("m") else "buy",
            ts=float(d.get("T") or time.time() * 1000) / 1000,
        )
        await _maybe(self.on_trade, tick)

    async def _book(self, d: dict, stream: str) -> None:
        raw_sym = stream.split("@")[0] if "@" in stream else d.get("s", "")
        bids = [BookLevel(float(p), float(q)) for p, q in d.get("bids", [])[:12]]
        asks = [BookLevel(float(p), float(q)) for p, q in d.get("asks", [])[:12]]
        book = OrderBook(
            exchange="binance",
            symbol=from_binance(raw_sym),
            bids=bids,
            asks=asks,
            ts=time.time(),
        )
        await _maybe(self.on_book, book)


class BybitFeed(WSClient):
    name = "bybit"

    async def _session(self) -> None:
        async with websockets.connect(self.url, ping_interval=20, ping_timeout=20, max_size=2**23) as ws:
            args = []
            for s in self.symbols:
                b = to_bybit(s)
                args.append(f"tickers.{b}")
                args.append(f"publicTrade.{b}")
                args.append(f"orderbook.1.{b}")
            await ws.send(json.dumps({"op": "subscribe", "args": args}))
            self.connected = True
            log.info("bybit subscribed %d args", len(args))
            async for raw in ws:
                if self._stop.is_set():
                    break
                self._mark()
                msg = json.loads(raw)
                topic = msg.get("topic", "")
                data = msg.get("data")
                if not data:
                    continue
                if topic.startswith("tickers."):
                    row = data if isinstance(data, dict) else data[0]
                    await self._ticker(row)
                elif topic.startswith("publicTrade."):
                    rows = data if isinstance(data, list) else [data]
                    for row in rows:
                        await self._trade(row)
                elif topic.startswith("orderbook."):
                    row = data if isinstance(data, dict) else data[0]
                    await self._book(row, topic)

    async def _ticker(self, d: dict) -> None:
        last = float(d.get("lastPrice") or d.get("markPrice") or 0)
        bid = float(d.get("bid1Price") or last or 0)
        ask = float(d.get("ask1Price") or last or 0)
        chg = float(d.get("price24hPcnt") or 0) * 100
        t = Ticker(
            exchange="bybit",
            symbol=from_binance(d.get("symbol", "")),
            last=last,
            bid=bid,
            ask=ask,
            volume=float(d.get("volume24h") or 0),
            ts=time.time(),
            high=float(d.get("highPrice24h") or 0),
            low=float(d.get("lowPrice24h") or 0),
            change_pct=chg,
        )
        await _maybe(self.on_ticker, t)

    async def _trade(self, d: dict) -> None:
        tick = TradeTick(
            exchange="bybit",
            symbol=from_binance(d.get("s", "")),
            price=float(d.get("p") or 0),
            qty=float(d.get("v") or 0),
            side=(d.get("S") or "buy").lower(),
            ts=float(d.get("T") or time.time() * 1000) / 1000,
        )
        await _maybe(self.on_trade, tick)

    async def _book(self, d: dict, topic: str) -> None:
        parts = topic.split(".")
        sym = from_binance(parts[-1]) if parts else ""
        bids = [BookLevel(float(p), float(q)) for p, q in d.get("b", [])[:12]]
        asks = [BookLevel(float(p), float(q)) for p, q in d.get("a", [])[:12]]
        await _maybe(
            self.on_book,
            OrderBook(exchange="bybit", symbol=sym, bids=bids, asks=asks, ts=time.time()),
        )


class OkxFeed(WSClient):
    name = "okx"

    async def _session(self) -> None:
        async with websockets.connect(self.url, ping_interval=20, ping_timeout=20, max_size=2**23) as ws:
            args = []
            for s in self.symbols:
                inst = to_okx(s)
                args.append({"channel": "tickers", "instId": inst})
                args.append({"channel": "trades", "instId": inst})
                args.append({"channel": "books5", "instId": inst})
            await ws.send(json.dumps({"op": "subscribe", "args": args}))
            self.connected = True
            ping_task = asyncio.create_task(self._ping(ws))
            try:
                async for raw in ws:
                    if self._stop.is_set():
                        break
                    if raw == "pong":
                        continue
                    self._mark()
                    msg = json.loads(raw)
                    arg = msg.get("arg") or {}
                    data = msg.get("data") or []
                    ch = arg.get("channel")
                    if ch == "tickers":
                        for row in data:
                            await self._ticker(row)
                    elif ch == "trades":
                        for row in data:
                            await self._trade(row)
                    elif ch == "books5":
                        for row in data:
                            await self._book(row, arg.get("instId", ""))
            finally:
                ping_task.cancel()

    async def _ping(self, ws) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(15)
            try:
                await ws.send("ping")
            except Exception:
                return

    async def _ticker(self, d: dict) -> None:
        last = float(d.get("last") or 0)
        t = Ticker(
            exchange="okx",
            symbol=from_okx(d.get("instId", "")),
            last=last,
            bid=float(d.get("bidPx") or last),
            ask=float(d.get("askPx") or last),
            volume=float(d.get("vol24h") or 0),
            ts=float(d.get("ts") or time.time() * 1000) / 1000,
            high=float(d.get("high24h") or 0),
            low=float(d.get("low24h") or 0),
            change_pct=0.0,
        )
        if t.last and t.high:
            # approx from open if present
            open_px = float(d.get("open24h") or 0)
            if open_px:
                t.change_pct = (t.last - open_px) / open_px * 100
        await _maybe(self.on_ticker, t)

    async def _trade(self, d: dict) -> None:
        tick = TradeTick(
            exchange="okx",
            symbol=from_okx(d.get("instId", "")),
            price=float(d.get("px") or 0),
            qty=float(d.get("sz") or 0),
            side=(d.get("side") or "buy").lower(),
            ts=float(d.get("ts") or time.time() * 1000) / 1000,
        )
        await _maybe(self.on_trade, tick)

    async def _book(self, d: dict, inst: str) -> None:
        bids = [BookLevel(float(x[0]), float(x[1])) for x in d.get("bids", [])[:12]]
        asks = [BookLevel(float(x[0]), float(x[1])) for x in d.get("asks", [])[:12]]
        await _maybe(
            self.on_book,
            OrderBook(exchange="okx", symbol=from_okx(inst), bids=bids, asks=asks, ts=time.time()),
        )


class CoinbaseFeed(WSClient):
    name = "coinbase"

    async def _session(self) -> None:
        products = [to_coinbase(s) for s in self.symbols if s in COINBASE_USD_MAP]
        if not products:
            log.info("coinbase: no mapped products in watchlist")
            await asyncio.sleep(30)
            return
        async with websockets.connect(self.url, ping_interval=20, ping_timeout=20, max_size=2**23) as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "subscribe",
                        "product_ids": products,
                        "channels": ["ticker", "matches"],
                    }
                )
            )
            self.connected = True
            log.info("coinbase subscribed %s", products[:8])
            async for raw in ws:
                if self._stop.is_set():
                    break
                self._mark()
                msg = json.loads(raw)
                t = msg.get("type")
                if t == "ticker":
                    await self._ticker(msg)
                elif t in ("match", "last_match"):
                    await self._trade(msg)

    async def _ticker(self, d: dict) -> None:
        last = float(d.get("price") or 0)
        open_24 = float(d.get("open_24h") or 0)
        chg = ((last - open_24) / open_24 * 100) if open_24 else 0.0
        t = Ticker(
            exchange="coinbase",
            symbol=from_coinbase(d.get("product_id", "")),
            last=last,
            bid=float(d.get("best_bid") or last),
            ask=float(d.get("best_ask") or last),
            volume=float(d.get("volume_24h") or 0),
            ts=time.time(),
            high=float(d.get("high_24h") or 0),
            low=float(d.get("low_24h") or 0),
            change_pct=chg,
        )
        await _maybe(self.on_ticker, t)

    async def _trade(self, d: dict) -> None:
        tick = TradeTick(
            exchange="coinbase",
            symbol=from_coinbase(d.get("product_id", "")),
            price=float(d.get("price") or 0),
            qty=float(d.get("size") or 0),
            side=(d.get("side") or "buy").lower(),
            ts=time.time(),
        )
        await _maybe(self.on_trade, tick)


class KrakenFeed(WSClient):
    name = "kraken"

    async def _session(self) -> None:
        pairs = [to_kraken(s) for s in self.symbols if s in KRAKEN_MAP]
        if not pairs:
            log.info("kraken: no mapped pairs in watchlist")
            await asyncio.sleep(30)
            return
        async with websockets.connect(self.url, ping_interval=20, ping_timeout=20, max_size=2**23) as ws:
            await ws.send(
                json.dumps({"event": "subscribe", "pair": pairs, "subscription": {"name": "ticker"}})
            )
            await ws.send(
                json.dumps({"event": "subscribe", "pair": pairs, "subscription": {"name": "trade"}})
            )
            self.connected = True
            log.info("kraken subscribed %s", pairs[:8])
            async for raw in ws:
                if self._stop.is_set():
                    break
                self._mark()
                msg = json.loads(raw)
                if isinstance(msg, dict):
                    continue
                if not isinstance(msg, list) or len(msg) < 3:
                    continue
                channel = msg[-2] if isinstance(msg[-2], str) else ""
                pair = msg[-1] if isinstance(msg[-1], str) else ""
                payload = msg[1]
                if "ticker" in channel:
                    await self._ticker(payload, pair)
                elif "trade" in channel:
                    await self._trade(payload, pair)

    async def _ticker(self, d: dict, pair: str) -> None:
        last = float(d.get("c", [0])[0])
        bid = float(d.get("b", [0])[0])
        ask = float(d.get("a", [0])[0])
        open_px = float(d.get("o", [0])[0])
        chg = ((last - open_px) / open_px * 100) if open_px else 0.0
        t = Ticker(
            exchange="kraken",
            symbol=from_kraken(pair),
            last=last,
            bid=bid,
            ask=ask,
            volume=float(d.get("v", [0, 0])[-1]),
            ts=time.time(),
            high=float(d.get("h", [0, 0])[-1]),
            low=float(d.get("l", [0, 0])[-1]),
            change_pct=chg,
        )
        await _maybe(self.on_ticker, t)

    async def _trade(self, rows: list, pair: str) -> None:
        for row in rows:
            # [price, volume, time, side, orderType, misc]
            tick = TradeTick(
                exchange="kraken",
                symbol=from_kraken(pair),
                price=float(row[0]),
                qty=float(row[1]),
                side="buy" if row[3] == "b" else "sell",
                ts=float(row[2]),
            )
            await _maybe(self.on_trade, tick)


BASE_PRICES = {
    "BTC/USDT": 97_450.0,
    "ETH/USDT": 4_180.0,
    "SOL/USDT": 178.4,
    "BNB/USDT": 612.0,
    "XRP/USDT": 2.42,
    "DOGE/USDT": 0.168,
    "ADA/USDT": 0.72,
    "AVAX/USDT": 28.6,
    "LINK/USDT": 18.4,
    "DOT/USDT": 6.15,
    "LTC/USDT": 84.2,
    "ATOM/USDT": 8.9,
    "UNI/USDT": 9.4,
    "APT/USDT": 8.1,
    "SUI/USDT": 1.62,
    "NEAR/USDT": 4.05,
    "FIL/USDT": 4.4,
    "ARB/USDT": 0.62,
    "OP/USDT": 1.18,
    "TIA/USDT": 6.4,
    "INJ/USDT": 22.8,
    "PEPE/USDT": 0.0000091,
    "SHIB/USDT": 0.000018,
    "TON/USDT": 5.35,
    "AAVE/USDT": 168.0,
}


def _weighted_pick(rng: random.Random, weighted: tuple[tuple[str, float], ...]) -> str:
    roll = rng.random() * sum(w for _, w in weighted)
    for name, weight in weighted:
        roll -= weight
        if roll <= 0:
            return name
    return weighted[-1][0]


def _base_price(symbol: str) -> float:
    if symbol in BASE_PRICES:
        return BASE_PRICES[symbol]
    digest = hashlib.sha256(symbol.encode()).digest()
    n = int.from_bytes(digest[:8], "big") / 2**64
    return 10 ** (n * 4 - 2)


class SimulatedFeed(WSClient):
    """Paper tape used when venue TLS is unreachable (air-gapped / filtered hosts)."""

    name = "sim"

    #: venues the paper tape pretends to consolidate, with a relative size weight.
    #: The ``sim:`` prefix is deliberate — these numbers are generated, and no
    #: table in the UI should ever imply they came off a real exchange.
    SIM_VENUES = (("sim:binance", 0.34), ("sim:okx", 0.19), ("sim:bybit", 0.18),
                  ("sim:mexc", 0.12), ("sim:kucoin", 0.09), ("sim:bitget", 0.08))

    async def _session(self) -> None:
        rng = random.Random(42)
        prices = {s: _base_price(s) for s in self.symbols}
        now = time.time()
        self.connected = True
        log.warning("simulated market tape online for %d symbols", len(self.symbols))
        # ~4 days of 1m candles so the 5m/15m/1h/4h multi-timeframe views all have
        # enough history to work offline (1d/1w need real REST history).
        seed_bars = 5760
        for s, px in prices.items():
            p = px
            base_vol = max(1.0, 4_000_000.0 / max(px, 1e-9)) * rng.uniform(0.6, 1.5)
            for i in range(seed_bars):
                ts = now - (seed_bars - i) * 60
                open_px = p
                p *= math.exp(rng.gauss(0.00004, 0.0035))
                # a real intrabar range, not a doji: wicks scale with the move
                move = abs(p - open_px)
                wick = max(move * rng.uniform(0.3, 1.6), open_px * rng.uniform(0.0004, 0.0022))
                high = max(open_px, p) + wick * rng.random()
                low = min(open_px, p) - wick * rng.random()
                # volume follows the range, with the usual heavy tail
                vol = base_vol * (0.4 + move / max(open_px * 0.004, 1e-12)) * rng.uniform(0.5, 1.8)
                if rng.random() < 0.02:
                    vol *= rng.uniform(3, 9)
                await _maybe(self.on_bar, s, ts, open_px, high, low, p, round(vol, 6))
                await self._emit(s, p, ts, rng, seed=True)
                prices[s] = p
        # order-flow state per symbol: a slow-moving aggressor bias, and the
        # occasional absorption episode where one side keeps hitting and price
        # refuses to move. Without these the tape is a coin flip and delta
        # carries no information at all.
        bias = {s: 0.0 for s in self.symbols}
        absorb = {s: 0 for s in self.symbols}
        while not self._stop.is_set():
            await asyncio.sleep(0.35)
            ts = time.time()
            for s in self.symbols:
                shock = rng.gauss(0, 0.00045)
                if rng.random() < 0.01:
                    shock += rng.choice([-1, 1]) * rng.uniform(0.002, 0.01)
                bias[s] = bias[s] * 0.97 + rng.gauss(0, 0.35)
                if absorb[s] > 0:
                    absorb[s] -= 1
                    shock *= 0.15          # the move is being absorbed
                elif rng.random() < 0.004:
                    absorb[s] = rng.randint(20, 60)
                    bias[s] = rng.choice([-1, 1]) * rng.uniform(1.4, 2.6)
                prices[s] *= math.exp(shock)
                await self._emit(s, prices[s], ts, rng, bias=bias[s], shock=shock)

    async def _emit(self, symbol: str, last: float, ts: float, rng: random.Random, seed: bool = False,
                    bias: float = 0.0, shock: float = 0.0) -> None:
        self._mark()
        spread = last * rng.uniform(0.00012, 0.0004)
        bid = last - spread / 2
        ask = last + spread / 2
        chg = rng.uniform(-3.5, 3.5)
        await _maybe(
            self.on_ticker,
            Ticker(
                exchange="sim",
                symbol=symbol,
                last=last,
                bid=bid,
                ask=ask,
                volume=abs(rng.gauss(1e6, 2e5)),
                ts=ts,
                high=last * 1.01,
                low=last * 0.99,
                change_pct=chg,
            ),
        )
        if seed:
            return
        # a burst of prints spread across the simulated venues. The aggressor
        # side follows the momentum and the slow bias, so cumulative delta
        # actually leads and lags price the way a real tape does.
        lean = math.tanh(bias * 0.5 + shock * 900)
        for _ in range(rng.randint(1, 4)):
            if rng.random() > 0.55:
                continue
            venue = _weighted_pick(rng, self.SIM_VENUES)
            buy = rng.random() < 0.5 + lean * 0.32
            # print sizes are fat-tailed: mostly dust, occasionally a whale
            qty = abs(rng.gauss(0.35, 0.18))
            if rng.random() < 0.03:
                qty *= rng.uniform(6, 30)
            await _maybe(
                self.on_trade,
                TradeTick(
                    exchange=venue,
                    symbol=symbol,
                    price=(ask if buy else bid) * (1 + rng.gauss(0, 0.00003)),
                    qty=round(qty, 6),
                    side="buy" if buy else "sell",
                    ts=ts,
                ),
            )
        bids = [BookLevel(bid - i * spread * 0.4, abs(rng.gauss(1.2, 0.4))) for i in range(10)]
        asks = [BookLevel(ask + i * spread * 0.4, abs(rng.gauss(1.2, 0.4))) for i in range(10)]
        await _maybe(
            self.on_book,
            OrderBook(exchange="sim", symbol=symbol, bids=bids, asks=asks, ts=ts),
        )
