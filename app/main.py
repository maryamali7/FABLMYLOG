from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, ORJSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import ROOT, load_settings
from app.engine import Robot
from app.market.hub import MarketHub
from app.market.rest import fetch_klines, fetch_universe
from app.screener import scan as scan_screener
from app.storage import Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("fablmylog")

WEB = ROOT / "web"
settings = load_settings()
store = Store()
hub = MarketHub(settings)
robot = Robot(settings, store, hub)
clients: set[WebSocket] = set()
broadcaster_task: asyncio.Task | None = None


async def broadcast(msg: dict[str, Any]) -> None:
    dead = []
    for ws in list(clients):
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


async def pulse() -> None:
    while True:
        try:
            state = robot.snapshot()
            await broadcast(
                {
                    "type": "state",
                    "state": state,
                    "tickers": hub.ticker_table(),
                    "signals": robot.signals,
                    "arb": hub.arb,
                    "tape": [
                        {
                            "exchange": t.exchange,
                            "symbol": t.symbol,
                            "price": t.price,
                            "qty": t.qty,
                            "side": t.side,
                            "ts": t.ts,
                        }
                        for t in list(hub.trades)[:40]
                    ],
                    "events": robot.notes[:30],
                    "screener": robot.screener,
                    "regime": robot.regime,
                    "alerts": robot.alerts[:12],
                    "ts": time.time(),
                }
            )
        except Exception as exc:
            log.debug("pulse: %s", exc)
        await asyncio.sleep(1.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global broadcaster_task
    await store.connect()
    robot.bind_broadcast(broadcast)
    await robot.boot()
    await robot.start()
    broadcaster_task = asyncio.create_task(pulse(), name="pulse")
    log.info("FablMyLog online  mode=%s  watch=%d", robot.mode, len(robot.watchlist))
    yield
    if broadcaster_task:
        broadcaster_task.cancel()
    await robot.stop()
    await hub.stop()
    await store.close()


app = FastAPI(title="FablMyLog", default_response_class=ORJSONResponse, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

if WEB.exists():
    app.mount("/static", StaticFiles(directory=str(WEB)), name="static")


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")


@app.get("/health")
async def health():
    return {"ok": True, "mode": robot.mode, "running": robot.running}


@app.get("/api/state")
async def api_state():
    return robot.snapshot()


@app.get("/api/tickers")
async def api_tickers():
    return hub.ticker_table()


@app.get("/api/universe")
async def api_universe(limit: int = 200):
    if robot.universe:
        return robot.universe[:limit]
    try:
        robot.universe = await fetch_universe(settings.quote_asset, 400)
    except Exception as exc:
        return {"error": str(exc), "rows": []}
    return robot.universe[:limit]


@app.get("/api/candles/{symbol:path}")
async def api_candles(symbol: str, interval: str = "1m"):
    symbol = symbol.upper().replace("-", "/")
    win = hub.candles.get(symbol)
    if win and len(win) > 5:
        rows = []
        for i in range(len(win)):
            rows.append(
                {
                    "ts": win.ts[i],
                    "open": win.opens[i],
                    "high": win.highs[i],
                    "low": win.lows[i],
                    "close": win.closes[i],
                    "volume": win.volumes[i],
                }
            )
        return rows
    try:
        kl = await fetch_klines(symbol, interval, 200)
        return [
            {"ts": c.ts, "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}
            for c in kl
        ]
    except Exception as exc:
        return {"error": str(exc)}


@app.get("/api/book/{symbol:path}")
async def api_book(symbol: str):
    symbol = symbol.upper().replace("-", "/")
    book = hub.books.get(symbol) or hub.books.get(f"binance:{symbol}")
    if not book:
        return {"bids": [], "asks": [], "symbol": symbol}
    return {
        "exchange": book.exchange,
        "symbol": book.symbol,
        "bids": [{"price": l.price, "qty": l.qty} for l in book.bids],
        "asks": [{"price": l.price, "qty": l.qty} for l in book.asks],
        "ts": book.ts,
    }


@app.get("/api/fills")
async def api_fills():
    return await store.recent_fills(100)


@app.get("/api/equity")
async def api_equity():
    return await store.equity_series(500)


class WatchBody(BaseModel):
    symbols: list[str]


class TradeBody(BaseModel):
    symbol: str
    side: str
    notional: float | None = None


class ToggleBody(BaseModel):
    name: str
    enabled: bool | None = None


class CloseBody(BaseModel):
    symbol: str


class ScreenerWatchBody(BaseModel):
    board: str = "alpha"
    n: int = 5
    symbol: str | None = None


@app.post("/api/watchlist")
async def api_watchlist(body: WatchBody):
    await robot.set_watchlist(body.symbols)
    return {"ok": True, "watchlist": robot.watchlist}


@app.post("/api/start")
async def api_start():
    robot.paused = False
    if not robot.running:
        await robot.start()
    return {"ok": True, "running": True}


@app.post("/api/pause")
async def api_pause():
    robot.paused = True
    await robot.emit("pause", {})
    return {"ok": True, "paused": True}


@app.post("/api/flatten")
async def api_flatten():
    await robot.flatten()
    return {"ok": True, "positions": 0}


@app.get("/api/screener")
async def api_screener():
    if not robot.screener:
        robot.screener = scan_screener(hub, robot.watchlist)
    return robot.screener


@app.post("/api/strategies/toggle")
async def api_toggle(body: ToggleBody):
    enabled = robot.toggle_strategy(body.name, body.enabled)
    if enabled is None:
        return {"ok": False, "error": "unknown strategy"}
    await robot.emit("strategy", {"name": body.name, "enabled": enabled})
    return {"ok": True, "name": body.name, "enabled": enabled}


@app.post("/api/close")
async def api_close(body: CloseBody):
    sym = body.symbol.upper().replace("-", "/")
    t = hub.quote(sym)
    px = t.last if t else 0
    await robot._close(sym, px, "manual close")
    return {"ok": True}


@app.post("/api/screener/watch")
async def api_screener_watch(body: ScreenerWatchBody):
    if body.symbol:
        symbols = list(dict.fromkeys([*robot.watchlist, body.symbol.upper().replace("-", "/")]))
    else:
        board = ((robot.screener or {}).get("boards") or {}).get(body.board) or []
        extra = [r["symbol"] for r in board[: max(1, min(body.n, 10))]]
        symbols = list(dict.fromkeys([*robot.watchlist, *extra]))
    await robot.set_watchlist(symbols)
    return {"ok": True, "watchlist": robot.watchlist}


@app.post("/api/manual")
async def api_manual(body: TradeBody):
    from app.models import Signal, SignalKind

    t = hub.quote(body.symbol.upper().replace("-", "/"))
    if not t:
        return {"ok": False, "error": "no market data yet"}
    kind = SignalKind.BUY if body.side.lower() == "buy" else SignalKind.SELL
    sig = Signal(
        strategy="manual",
        symbol=t.symbol,
        kind=kind,
        confidence=0.9,
        price=t.last,
        reason="manual ticket",
        ts=time.time(),
    )
    if kind == SignalKind.SELL:
        await robot._close(t.symbol, t.last, "manual sell")
    else:
        # temporarily size via notional by faking confidence if provided
        await robot._maybe_enter(sig, t)
    return {"ok": True}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    try:
        await ws.send_json({"type": "hello", "state": robot.snapshot(), "tickers": hub.ticker_table()})
        while True:
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_json({"type": "pong", "ts": time.time()})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        clients.discard(ws)


def run() -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    run()
