from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, ORJSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.alerts import ALERT_TEMPLATES
from app.backtest import backtest, compare, portfolio_backtest
from app.config import ROOT, load_settings
from app.custom import TEMPLATES, template as custom_template, validate_spec
from app.engine import Robot
from app.market.hub import MarketHub
from app.market.rest import fetch_klines, fetch_universe
from app.market.venues import MARKETS, VENUES
from app.universe import PRESETS as UNIVERSE_PRESETS, SORTS as UNIVERSE_SORTS
from app.predict import HORIZONS
from app.rules import COMPARATORS, field_catalog
from app.screener import (
    BOARD_META,
    DEFAULT_COLUMNS,
    PRESETS,
    rows_to_csv,
    run_query,
    summarize,
)
from app.screener import scan as scan_screener
from app.storage import Store
from app.strategies import REGISTRY
from app.timeframes import TF_LABEL, TF_ORDER

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
                    "rule_alerts": robot.rule_alerts[:12],
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
    return FileResponse(WEB / "index.html", media_type="text/html; charset=utf-8")


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
    """Legacy flat list (spot pairs on the configured quote asset)."""
    if robot.universe:
        return robot.universe[:limit]
    if robot.instruments.rows:
        page = robot.instruments.query(market="spot", quote=settings.quote_asset, limit=limit)
        return [
            {
                "symbol": r["symbol"],
                "last": r["last"],
                "change_pct": r["change_pct"],
                "volume": r["volume_usd"],
                "venue": r["venue"],
            }
            for r in page["rows"]
        ]
    try:
        robot.universe = await fetch_universe(settings.quote_asset, 400)
    except Exception as exc:
        return {"error": str(exc), "rows": []}
    return robot.universe[:limit]


class InstrumentWatchBody(BaseModel):
    symbol: str | None = None
    symbols: list[str] = []


# --------------------------------------------------------------------------- #
# multi-venue instrument universe (binance / bybit / okx / mexc · spot + perp)
# --------------------------------------------------------------------------- #


@app.get("/api/instruments")
async def api_instruments(
    venue: str = "",
    market: str = "",
    quote: str = "",
    search: str = "",
    sort: str = "volume",
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    min_volume: float = 0.0,
    max_volume: float = 0.0,
    change_min: float | None = None,
    change_max: float | None = None,
    funding_min: float | None = None,
    funding_max: float | None = None,
    preset: str = "",
):
    """Every listed instrument across venues and market types."""
    if robot.instruments.stale and not robot.instruments.loading:
        await robot.instruments.refresh()
    return robot.instruments.query(
        venue=venue,
        market=market,
        quote=quote,
        search=search,
        sort=sort,
        limit=limit,
        offset=offset,
        min_volume=min_volume,
        max_volume=max_volume,
        change_min=change_min,
        change_max=change_max,
        funding_min=funding_min,
        funding_max=funding_max,
        preset=preset,
    )


@app.get("/api/instruments/presets")
async def api_instruments_presets():
    return {
        "presets": UNIVERSE_PRESETS,
        "venues": list(VENUES),
        "markets": list(MARKETS),
        "sorts": sorted(UNIVERSE_SORTS),
    }


@app.get("/api/instruments/carry")
async def api_instruments_carry(
    quote: str = "USDT", limit: int = Query(20, ge=1, le=100), min_volume: float = 1e6
):
    """Cash-and-carry ranking: cheapest spot venue vs the best-paying perp."""
    return {
        "rows": robot.instruments.carry(quote=quote, limit=limit, min_volume=min_volume),
        "source": robot.instruments.report.get("source"),
        "note": "funding APR + basis, before fees, borrow and slippage",
    }


@app.get("/api/instruments/exclusives")
async def api_instruments_exclusives(limit: int = Query(30, ge=1, le=200), min_volume: float = 0.0):
    """Coins listed on exactly one venue."""
    return {
        "rows": robot.instruments.exclusives(limit=limit, min_volume=min_volume),
        "source": robot.instruments.report.get("source"),
    }


@app.get("/api/instruments/movers")
async def api_instruments_movers(
    quote: str = "USDT", limit: int = Query(15, ge=1, le=100), min_volume: float = 2e6
):
    """Best and worst 24h movers across every venue, one row per coin."""
    return robot.instruments.movers(quote=quote, limit=limit, min_volume=min_volume)


@app.get("/api/instruments/stats")
async def api_instruments_stats():
    if robot.instruments.stale and not robot.instruments.loading:
        await robot.instruments.refresh()
    return robot.instruments.stats()


@app.post("/api/instruments/refresh")
async def api_instruments_refresh(venue: str = "", market: str = ""):
    venues = [v for v in venue.split(",") if v] or list(VENUES)
    markets = [m for m in market.split(",") if m] or list(MARKETS)
    report = await robot.instruments.refresh(venues, markets, force=True)
    return {"ok": True, "report": report, "stats": robot.instruments.stats()}


@app.get("/api/instruments/coins")
async def api_instruments_coins(
    quote: str = "USDT",
    limit: int = Query(100, ge=1, le=500),
    min_venues: int = Query(1, ge=1, le=8),
):
    """One row per coin, merged across every venue and market."""
    if robot.instruments.stale and not robot.instruments.loading:
        await robot.instruments.refresh()
    return {
        "rows": robot.instruments.coins(quote=quote, limit=limit, min_venues=min_venues),
        "source": robot.instruments.report.get("source"),
    }


@app.get("/api/instruments/arb")
async def api_instruments_arb(
    quote: str = "USDT",
    market: str = "spot",
    limit: int = Query(20, ge=1, le=100),
    min_volume: float = 1e6,
):
    return {
        "rows": robot.instruments.arbitrage(quote=quote, market=market, limit=limit, min_volume=min_volume),
        "source": robot.instruments.report.get("source"),
    }


@app.get("/api/instruments/funding")
async def api_instruments_funding(
    quote: str = "USDT", limit: int = Query(15, ge=1, le=100), min_volume: float = 1e6
):
    return robot.instruments.funding(quote=quote, limit=limit, min_volume=min_volume)


@app.get("/api/instruments/symbol/{symbol:path}")
async def api_instrument_detail(symbol: str):
    """Every venue/market listing for one symbol, with the cross-venue spread."""
    rows = robot.instruments.find(symbol)
    prices = [r["last"] for r in rows if r["last"] > 0]
    return {
        "symbol": symbol.upper().replace("-", "/"),
        "listings": rows,
        "venues": sorted({r["venue"] for r in rows}),
        "markets": sorted({r["market"] for r in rows}),
        "spread_pct": round((max(prices) / min(prices) - 1) * 100, 4) if len(prices) > 1 else 0.0,
        "in_watchlist": symbol.upper().replace("-", "/") in robot.watchlist,
    }


@app.get("/api/instruments/export.csv")
async def api_instruments_csv(
    venue: str = "", market: str = "", quote: str = "", search: str = "", limit: int = Query(500, ge=1, le=5000)
):
    page = robot.instruments.query(
        venue=venue, market=market, quote=quote, search=search, limit=limit
    )
    return PlainTextResponse(
        robot.instruments.to_csv(page["rows"]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=fablmylog-instruments.csv"},
    )


@app.post("/api/instruments/watch")
async def api_instruments_watch(body: InstrumentWatchBody):
    """Add instruments from the universe browser to the live watchlist."""
    symbols = [s for s in (body.symbols or []) if s]
    if body.symbol:
        symbols.append(body.symbol)
    if not symbols:
        return {"ok": False, "error": "no symbols given", "watchlist": robot.watchlist}
    before = set(robot.watchlist)
    watchlist = await robot.add_symbols(symbols)
    added = [s for s in watchlist if s not in before]
    return {
        "ok": True,
        "added": added,
        "skipped": [s.upper().replace("-", "/") for s in symbols if s.upper().replace("-", "/") not in watchlist],
        "watchlist": watchlist,
        "cap": settings.max_watch_symbols,
    }


@app.get("/api/candles/{symbol:path}")
async def api_candles(symbol: str, interval: str = "1m"):
    symbol = symbol.upper().replace("-", "/")
    if interval != "1m":
        rows = robot.mtf.best_frame(symbol, interval)
        if rows and len(rows) > 5:
            return rows
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


# --------------------------------------------------------------------------- #
# strategy builder
# --------------------------------------------------------------------------- #


class SpecBody(BaseModel):
    spec: dict[str, Any]


class BacktestBody(BaseModel):
    spec: dict[str, Any] | None = None
    builtin: str | None = None
    symbol: str = "BTC/USDT"
    interval: str = "1m"
    bars: int = 600
    config: dict[str, Any] | None = None
    symbols: list[str] | None = None
    compare_with: list[str] | None = None


class ScreenBody(BaseModel):
    filters: Any = None
    sort_by: str = "alpha"
    sort_dir: str = "desc"
    limit: int = 60
    search: str = ""
    preset: str | None = None
    match: str = "all"


class AlertBody(BaseModel):
    spec: dict[str, Any]


class RiskBody(BaseModel):
    patch: dict[str, Any]


@app.get("/api/builder/catalog")
async def api_builder_catalog():
    """Everything the visual builder needs to render: fields, operators, templates."""
    return {
        "fields": field_catalog(),
        "comparators": [{"op": k, **v} for k, v in COMPARATORS.items()],
        "group_ops": ["all", "any", "none"],
        "templates": TEMPLATES,
        "builtins": sorted(REGISTRY.keys()),
        "alert_templates": ALERT_TEMPLATES,
        "boards": BOARD_META,
        "presets": PRESETS,
        "columns": DEFAULT_COLUMNS,
        "timeframes": [{"tf": tf, "label": TF_LABEL[tf]} for tf in TF_ORDER],
        "horizons": HORIZONS,
    }


@app.get("/api/strategies/custom")
async def api_custom_list():
    return {"strategies": robot.custom.list()}


@app.post("/api/strategies/custom")
async def api_custom_save(body: SpecBody):
    strat, errors = robot.save_custom(body.spec)
    if errors:
        return {"ok": False, "errors": errors}
    await robot.emit("strategy_saved", {"id": strat.spec["id"], "name": strat.title})
    return {"ok": True, "strategy": strat.to_dict()}


@app.post("/api/strategies/custom/validate")
async def api_custom_validate(body: SpecBody):
    errors = validate_spec(body.spec)
    if errors:
        return {"ok": False, "errors": errors, "matches": []}
    return robot.preview_custom(body.spec)


@app.post("/api/strategies/custom/{spec_id}/toggle")
async def api_custom_toggle(spec_id: str):
    enabled = robot.toggle_custom(spec_id)
    if enabled is None:
        return {"ok": False, "error": "unknown strategy"}
    return {"ok": True, "enabled": enabled}


@app.post("/api/strategies/custom/{spec_id}/duplicate")
async def api_custom_duplicate(spec_id: str):
    strat = robot.custom.duplicate(spec_id)
    if not strat:
        return {"ok": False, "error": "unknown strategy"}
    robot.refresh_strategies()
    return {"ok": True, "strategy": strat.to_dict()}


@app.delete("/api/strategies/custom/{spec_id}")
async def api_custom_delete(spec_id: str):
    return {"ok": robot.delete_custom(spec_id)}


@app.get("/api/strategies/templates")
async def api_templates(template_id: str | None = None):
    if template_id:
        spec = custom_template(template_id)
        return {"ok": bool(spec), "spec": spec}
    return {"templates": TEMPLATES}


async def _candles_for(symbol: str, interval: str, bars: int):
    """Prefer live hub candles, fall back to REST history (and back again)."""
    sym = symbol.upper().replace("-", "/")
    win = hub.candles.get(sym)
    if win and len(win) >= max(120, bars) and interval == settings.candle_interval:
        return win
    try:
        rows = await fetch_klines(sym, interval, min(max(bars, 120), 1000))
        if rows:
            return rows
    except Exception as exc:
        log.debug("kline fetch %s: %s", sym, exc)
    if win and len(win) >= 70:
        return win
    raise RuntimeError(f"no candle history available for {sym} yet — let the feed warm up")


@app.post("/api/backtest")
async def api_backtest(body: BacktestBody):
    try:
        if body.symbols:
            series = {}
            for sym in body.symbols[:8]:
                series[sym.upper().replace("-", "/")] = await _candles_for(sym, body.interval, body.bars)
            return portfolio_backtest(series, spec=body.spec, builtin=body.builtin, config=body.config)
        candles = await _candles_for(body.symbol, body.interval, body.bars)
        result = backtest(
            candles,
            spec=body.spec,
            builtin=body.builtin,
            symbol=body.symbol.upper().replace("-", "/"),
            config=body.config,
        )
        if body.compare_with:
            result["comparison"] = compare(
                candles, list(body.compare_with)[:6], body.symbol, body.config
            )
        return result
    except Exception as exc:
        return {"ok": False, "error": str(exc), "metrics": {}}


# --------------------------------------------------------------------------- #
# advanced screener
# --------------------------------------------------------------------------- #


def _screener_rows() -> list[dict[str, Any]]:
    if not robot.screener:
        robot.screener = scan_screener(hub, robot.watchlist)
    return (robot.screener or {}).get("rows") or []


@app.post("/api/screener/query")
async def api_screener_query(body: ScreenBody):
    rows = _screener_rows()
    result = run_query(
        rows,
        filters=body.filters,
        sort_by=body.sort_by,
        sort_dir=body.sort_dir,
        limit=body.limit,
        search=body.search,
        preset_id=body.preset,
        match=body.match,
    )
    result["summary"] = summarize(rows)
    result["scanned"] = len(rows)
    return result


@app.get("/api/screener/presets")
async def api_screener_presets():
    return {"presets": PRESETS, "columns": DEFAULT_COLUMNS, "boards": BOARD_META}


@app.get("/api/screener/export.csv")
async def api_screener_export(board: str = "", preset: str = "", limit: int = 200):
    rows = _screener_rows()
    if board:
        rows = ((robot.screener or {}).get("boards") or {}).get(board) or []
    elif preset:
        rows = run_query(rows, preset_id=preset, limit=limit)["rows"]
    csv_text = rows_to_csv(rows[:limit])
    return PlainTextResponse(
        csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=fablmylog-screener.csv"},
    )


@app.get("/api/screener/symbol/{symbol:path}")
async def api_screener_symbol(symbol: str):
    sym = symbol.upper().replace("-", "/")
    for row in _screener_rows():
        if row.get("symbol") == sym:
            return row
    return {"error": "symbol not scanned", "symbol": sym}


# --------------------------------------------------------------------------- #
# multi-timeframe analysis + next-move forecasting
# --------------------------------------------------------------------------- #


@app.get("/api/mtf")
async def api_mtf_scan(limit: int = Query(40, ge=1, le=200)):
    """Alignment table across every tracked symbol."""
    return {
        "ts": time.time(),
        "timeframes": [{"tf": tf, "label": TF_LABEL[tf]} for tf in TF_ORDER],
        "rows": robot.mtf.scan(robot.watchlist)[:limit],
        "ready": len({k[0] for k in robot.mtf.metrics}),
    }


@app.get("/api/mtf/{symbol:path}")
async def api_mtf_symbol(symbol: str, refresh: bool = False):
    sym = symbol.upper().replace("-", "/")
    if refresh:
        await robot.mtf.refresh_symbol(sym, force=True)
    snap = robot.mtf.snapshot(sym)
    if not snap.get("frames"):
        await robot.mtf.refresh_symbol(sym, force=True)
        snap = robot.mtf.snapshot(sym)
    snap["forecast"] = _slim_forecast(robot.forecasts.get(sym))
    return snap


@app.post("/api/mtf/refresh")
async def api_mtf_refresh(symbol: str | None = None):
    targets = [symbol.upper().replace("-", "/")] if symbol else robot.watchlist[:8]
    done = []
    for sym in targets:
        try:
            await robot.mtf.refresh_symbol(sym, force=True)
            done.append(sym)
        except Exception as exc:
            log.debug("mtf refresh %s: %s", sym, exc)
    return {"ok": True, "refreshed": done}


def _slim_forecast(f: dict[str, Any] | None) -> dict[str, Any] | None:
    if not f:
        return None
    keep = (
        "symbol",
        "timeframe",
        "direction",
        "probability_up",
        "probability_down",
        "expected_move_pct",
        "target",
        "upper",
        "lower",
        "confidence",
        "risk_reward",
        "horizon_label",
    )
    return {k: f[k] for k in keep if k in f}


@app.get("/api/predict/{symbol:path}")
async def api_predict(
    symbol: str,
    tf: str = Query("1m"),
    horizon: int | None = Query(None, ge=1, le=200),
):
    if tf not in TF_ORDER:
        return {"ok": False, "error": f"unknown timeframe {tf}", "timeframes": TF_ORDER}
    sym = symbol.upper().replace("-", "/")
    if tf != "1m" and not robot.mtf.best_frame(sym, tf):
        await robot.mtf.refresh_symbol(sym, force=True)
    return robot.forecast(sym, timeframe=tf, horizon=horizon)


@app.get("/api/forecasts")
async def api_forecasts(limit: int = Query(12, ge=1, le=60)):
    board = robot.forecast_board or {}
    return {
        "ts": time.time(),
        "up": (board.get("up") or [])[:limit],
        "down": (board.get("down") or [])[:limit],
        "all": (board.get("all") or [])[:limit],
        "cached": len(robot.forecasts),
    }


@app.get("/api/forecasts/accuracy")
async def api_forecast_accuracy(limit: int = Query(400, ge=20, le=1000)):
    """Scoreboard: how well the prediction ensemble has actually done."""
    return robot.tracker.stats(limit=limit)


@app.post("/api/forecasts/backfill")
async def api_forecast_backfill(symbol: str | None = None, points: int = Query(8, ge=1, le=40)):
    """Re-seed the scoreboard from candle history (no look-ahead)."""
    targets = [symbol.upper().replace("-", "/")] if symbol else None
    graded = await robot.backfill_scoreboard(targets, points=points)
    return {"ok": True, "graded": graded, "stats": robot.tracker.stats(limit=200)}


@app.get("/api/levels/{symbol:path}")
async def api_levels(symbol: str, tf: str = Query("1m")):
    sym = symbol.upper().replace("-", "/")
    out = robot.forecast(sym, timeframe=tf)
    if not out.get("ok"):
        return out
    return {
        "ok": True,
        "symbol": sym,
        "timeframe": tf,
        "price": out.get("price"),
        "levels": out.get("levels"),
        "regime": out.get("regime"),
    }


# --------------------------------------------------------------------------- #
# alert rules
# --------------------------------------------------------------------------- #


@app.get("/api/alerts/rules")
async def api_alert_rules():
    return {"rules": robot.alert_engine.list(), "templates": ALERT_TEMPLATES}


@app.post("/api/alerts/rules")
async def api_alert_save(body: AlertBody):
    rule, errors = robot.alert_engine.upsert(body.spec)
    if errors:
        return {"ok": False, "errors": errors}
    return {"ok": True, "rule": rule}


@app.post("/api/alerts/rules/{rule_id}/toggle")
async def api_alert_toggle(rule_id: str):
    enabled = robot.alert_engine.toggle(rule_id)
    if enabled is None:
        return {"ok": False, "error": "unknown rule"}
    return {"ok": True, "enabled": enabled}


@app.delete("/api/alerts/rules/{rule_id}")
async def api_alert_delete(rule_id: str):
    return {"ok": robot.alert_engine.delete(rule_id)}


@app.get("/api/alerts/history")
async def api_alert_history(limit: int = Query(40, ge=1, le=200)):
    return {"alerts": robot.alert_engine.recent(limit), "feed": robot.rule_alerts[:limit]}


# --------------------------------------------------------------------------- #
# analytics + risk
# --------------------------------------------------------------------------- #


@app.get("/api/analytics")
async def api_analytics():
    return await robot.analytics()


@app.get("/api/risk")
async def api_risk_get():
    return {"risk": settings.risk.model_dump(), "halted": robot.risk.halted, "reason": robot.risk.halt_reason}


@app.post("/api/risk")
async def api_risk_set(body: RiskBody):
    applied = robot.update_risk(body.patch)
    await robot.emit("risk", applied)
    return {"ok": True, "applied": applied, "risk": settings.risk.model_dump()}


@app.post("/api/risk/resume")
async def api_risk_resume():
    robot.risk.reset_day(robot.mark_equity)
    await robot.emit("risk_reset", {"equity": robot.mark_equity})
    return {"ok": True, "halted": robot.risk.halted}


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
