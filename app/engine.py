from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from app.config import Settings, has_live_keys
from app.indicators import atr, ema, roc
from app.market.hub import MarketHub
from app.market.rest import fetch_klines, fetch_universe, ping_exchanges
from app.models import Fill, Position, Side, Signal, SignalKind, Ticker
from app.screener import scan as scan_screener
from app.storage import Store
from app.strategies import build_strategies, ensemble

log = logging.getLogger("engine")


class RiskGate:
    def __init__(self, settings: Settings):
        self.cfg = settings.risk
        self.day_start_equity = settings.starting_equity
        self.peak_equity = settings.starting_equity
        self.last_loss_ts = 0.0
        self.halted = False
        self.halt_reason = ""

    def mark_equity(self, equity: float) -> None:
        self.peak_equity = max(self.peak_equity, equity)
        dd = (self.peak_equity - equity) / self.peak_equity if self.peak_equity else 0
        day_loss = (self.day_start_equity - equity) / self.day_start_equity if self.day_start_equity else 0
        if dd >= self.cfg.max_drawdown_pct:
            self.halted = True
            self.halt_reason = f"max drawdown {dd:.2%} hit"
        if day_loss >= self.cfg.max_daily_loss_pct:
            self.halted = True
            self.halt_reason = f"daily loss {day_loss:.2%} hit"

    def reset_day(self, equity: float) -> None:
        self.day_start_equity = equity
        self.halted = False
        self.halt_reason = ""

    def allow_entry(
        self,
        ticker: Ticker,
        positions: dict[str, Position],
        equity: float,
        notional: float,
    ) -> tuple[bool, str]:
        if self.halted:
            return False, self.halt_reason
        if time.time() - self.last_loss_ts < self.cfg.cooldown_after_loss_sec:
            return False, "cooldown after loss"
        if ticker.symbol in positions:
            return False, "already in position"
        if len(positions) >= self.cfg.max_open_positions:
            return False, "max open positions"
        if ticker.spread_bps > self.cfg.max_spread_bps:
            return False, f"spread {ticker.spread_bps:.1f} bps too wide"
        if notional > equity * self.cfg.max_position_pct * 1.05:
            return False, "size exceeds cap"
        if not ticker.last:
            return False, "no price"
        return True, "ok"

    def size(self, equity: float, confidence: float) -> float:
        base = equity * self.cfg.max_position_pct
        scale = 0.55 + 0.45 * max(0.0, min(1.0, (confidence - 0.5) / 0.5))
        return base * scale


class Robot:
    def __init__(self, settings: Settings, store: Store, hub: MarketHub):
        self.settings = settings
        self.store = store
        self.hub = hub
        self.risk = RiskGate(settings)
        self.strategies = build_strategies(settings.strategies)
        self.cash = settings.starting_equity
        self.positions: dict[str, Position] = {}
        self.signals: list[dict[str, Any]] = []
        self.running = False
        self.paused = False
        self.started_at = 0.0
        self.last_loop = 0.0
        self.loops = 0
        self.watchlist = list(settings.watchlist)
        self.universe: list[dict[str, Any]] = []
        self.rest_ok: dict[str, bool] = {}
        self._task: asyncio.Task | None = None
        self._broadcast = None
        self.wins = 0
        self.losses = 0
        self.realized = 0.0
        self.mode = settings.mode
        self.notes: list[dict[str, Any]] = []
        self.screener: dict[str, Any] = {}
        self.alerts: list[dict[str, Any]] = []
        self.strategy_pnl: dict[str, float] = {}
        self.regime: dict[str, Any] = {"name": "unknown", "risk_on": True}

    def bind_broadcast(self, fn) -> None:
        self._broadcast = fn

    async def emit(self, kind: str, payload: dict[str, Any]) -> None:
        event = {"ts": time.time(), "kind": kind, "payload": payload}
        self.notes.insert(0, event)
        self.notes = self.notes[:80]
        try:
            await self.store.add_event(kind, payload)
        except Exception:
            pass
        if self._broadcast:
            await self._broadcast({"type": "event", **event})

    @property
    def exposure(self) -> float:
        total = 0.0
        for p in self.positions.values():
            t = self.hub.quote(p.symbol)
            px = t.last if t else p.entry
            total += p.qty * px
        return total

    @property
    def unrealized(self) -> float:
        return sum(self._mtm(p) for p in self.positions.values())

    @property
    def mark_equity(self) -> float:
        # cash is post-entry; add marked position value
        return self.cash + self.exposure

    def _mtm(self, p: Position) -> float:
        t = self.hub.quote(p.symbol)
        px = t.last if t else p.entry
        if p.side == Side.BUY:
            return (px - p.entry) * p.qty
        return (p.entry - px) * p.qty

    async def boot(self) -> None:
        await self.emit("boot", {"mode": self.mode, "watchlist": self.watchlist})
        try:
            self.universe = await fetch_universe(self.settings.quote_asset, 400)
        except Exception as exc:
            log.warning("universe fetch failed: %s", exc)
            self.universe = [{"symbol": s, "last": 0, "change_pct": 0, "volume": 0} for s in self.watchlist]
        try:
            self.rest_ok = await ping_exchanges()
        except Exception:
            self.rest_ok = {}
        async def _seed(sym: str) -> None:
            try:
                kl = await fetch_klines(sym, self.settings.candle_interval, 180)
                win = self.hub.candles[sym]
                for c in kl:
                    win.push(c.ts, c.open, c.high, c.low, c.close, c.volume)
            except Exception as exc:
                log.warning("kline seed %s failed: %s", sym, exc)

        await asyncio.gather(*[_seed(sym) for sym in self.watchlist])
        await self.hub.start(self.watchlist)
        self.risk.day_start_equity = self.mark_equity
        self.risk.peak_equity = self.mark_equity

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.paused = False
        self.started_at = time.time()
        self._task = asyncio.create_task(self._loop(), name="robot-loop")
        await self.emit("start", {"mode": self.mode})

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        await self.emit("stop", {})

    async def _loop(self) -> None:
        while self.running:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("loop error: %s", exc)
                await self.emit("error", {"error": str(exc)})
            await asyncio.sleep(self.settings.loop_interval_sec)

    async def step(self) -> None:
        self.loops += 1
        self.last_loop = time.time()
        equity = self.mark_equity
        self.risk.mark_equity(equity)
        if self.loops % 15 == 0:
            try:
                await self.store.add_equity(equity, self.cash, self.exposure)
            except Exception:
                pass

        # manage open risk first
        for sym in list(self.positions.keys()):
            await self._manage_position(sym)

        self.regime = self._compute_regime()
        if self.loops % 2 == 0:
            try:
                self.screener = scan_screener(self.hub, self.watchlist)
                for a in self.screener.get("alerts") or []:
                    self.alerts.insert(0, a)
                self.alerts = self.alerts[:40]
            except Exception as exc:
                log.debug("screener: %s", exc)

        if self.paused or self.risk.halted:
            return

        fresh_signals: list[dict[str, Any]] = []
        for sym in self.watchlist:
            t = self.hub.quote(sym)
            if not t:
                continue
            win = self.hub.candles[sym]
            raw: list[Signal] = []
            for strat in self.strategies:
                if not strat.enabled:
                    continue
                try:
                    sig = strat.evaluate(sym, win, t.last)
                except Exception as exc:
                    log.debug("strategy %s failed: %s", strat.name, exc)
                    continue
                if sig:
                    raw.append(sig)
                    fresh_signals.append(sig.to_dict())
            combined = ensemble(raw, self.settings.risk.min_confidence)
            if combined:
                fresh_signals.append(combined.to_dict())
                await self._maybe_enter(combined, t)

        self.signals = fresh_signals[:80]

    async def _maybe_enter(self, sig: Signal, t: Ticker) -> None:
        if sig.kind not in (SignalKind.BUY, SignalKind.SELL):
            return
        # long-only by default in paper for spot (short = sell only if we hold)
        if sig.kind == SignalKind.SELL and sig.symbol not in self.positions:
            return
        if sig.kind == SignalKind.SELL and sig.symbol in self.positions:
            await self._close(sig.symbol, t.last, f"signal {sig.reason}")
            return
        notional = self.risk.size(self.mark_equity, sig.confidence)
        if not self.regime.get("risk_on", True):
            notional *= 0.55
        wr = self.wins / max(1, self.wins + self.losses)
        if self.wins + self.losses >= 8:
            kelly = max(0.45, min(1.15, 0.5 + (wr - 0.5)))
            notional *= kelly
        ok, why = self.risk.allow_entry(t, self.positions, self.mark_equity, notional)
        if not ok:
            return
        px = self._fill_price(t, Side.BUY)
        fee = notional * (self.settings.risk.fee_bps / 10_000)
        if self.cash < notional + fee:
            return
        qty = notional / px
        stop = px * (1 - self.settings.risk.stop_loss_pct)
        take = px * (1 + self.settings.risk.take_profit_pct)
        atr_val = 0.0
        win = self.hub.candles.get(sig.symbol)
        if win and len(win) >= 20:
            a = atr(list(win.highs), list(win.lows), list(win.closes))[-1]
            if a and a == a:
                atr_val = float(a)
                stop = min(stop, px - 1.6 * atr_val)
                take = max(take, px + 2.4 * atr_val)
        pos = Position(
            symbol=sig.symbol,
            side=Side.BUY,
            qty=qty,
            entry=px,
            stop=stop,
            take=take,
            trail=px,
            opened_ts=time.time(),
            strategy=sig.strategy,
            exchange=t.exchange,
            peak=px,
            atr=atr_val,
        )
        self.cash -= notional + fee
        self.positions[sig.symbol] = pos
        fill = Fill(
            id=uuid.uuid4().hex[:12],
            symbol=sig.symbol,
            side=Side.BUY,
            qty=qty,
            price=px,
            fee=fee,
            ts=time.time(),
            strategy=sig.strategy,
            exchange=t.exchange if self.mode == "paper" else t.exchange,
            paper=self.mode != "live",
            reason=sig.reason,
        )
        await self.store.add_fill(fill)
        await self.emit(
            "fill",
            {
                **fill.to_dict(),
                "stop": stop,
                "take": take,
                "confidence": sig.confidence,
            },
        )

    def _fill_price(self, t: Ticker, side: Side) -> float:
        slip = self.settings.risk.slippage_bps / 10_000
        if side == Side.BUY:
            px = t.ask or t.last
            return px * (1 + slip)
        px = t.bid or t.last
        return px * (1 - slip)

    async def _manage_position(self, symbol: str) -> None:
        pos = self.positions.get(symbol)
        t = self.hub.quote(symbol)
        if not pos or not t:
            return
        px = t.last
        pos.unrealized = self._mtm(pos)
        if pos.side == Side.BUY:
            pos.peak = max(pos.peak, px)
            trail_stop = pos.peak * (1 - self.settings.risk.trailing_stop_pct)
            if pos.atr:
                trail_stop = max(trail_stop, pos.peak - 1.2 * pos.atr)
            pos.trail = max(pos.stop, trail_stop)
            if not pos.scaled and px >= pos.entry + 0.5 * (pos.take - pos.entry):
                await self._scale_out(symbol, 0.5, "partial take 50% at 0.5R")
                return
            if px <= pos.trail:
                await self._close(symbol, px, "stop / trailing stop")
                return
            if px >= pos.take:
                await self._close(symbol, px, "take profit")
                return

    async def _close(self, symbol: str, price: float, reason: str) -> None:
        pos = self.positions.pop(symbol, None)
        if not pos:
            return
        t = self.hub.quote(symbol)
        px = self._fill_price(t, Side.SELL) if t else price
        notional = pos.qty * px
        fee = notional * (self.settings.risk.fee_bps / 10_000)
        pnl = (px - pos.entry) * pos.qty - fee
        # restore cash: we spent entry*qty+entry_fee, receive exit notional - fee
        self.cash += notional - fee
        self.realized += pnl
        self.strategy_pnl[pos.strategy] = self.strategy_pnl.get(pos.strategy, 0.0) + pnl
        if pnl >= 0:
            self.wins += 1
        else:
            self.losses += 1
            self.risk.last_loss_ts = time.time()
        fill = Fill(
            id=uuid.uuid4().hex[:12],
            symbol=symbol,
            side=Side.SELL,
            qty=pos.qty,
            price=px,
            fee=fee,
            ts=time.time(),
            strategy=pos.strategy,
            exchange=pos.exchange,
            paper=self.mode != "live",
            pnl=pnl,
            reason=reason,
        )
        await self.store.add_fill(fill)
        await self.emit("fill", {**fill.to_dict(), "pnl": pnl})

    async def _scale_out(self, symbol: str, frac: float, reason: str) -> None:
        pos = self.positions.get(symbol)
        t = self.hub.quote(symbol)
        if not pos or not t or frac <= 0 or frac >= 1:
            return
        qty = pos.qty * frac
        px = self._fill_price(t, Side.SELL)
        notional = qty * px
        fee = notional * (self.settings.risk.fee_bps / 10_000)
        pnl = (px - pos.entry) * qty - fee
        self.cash += notional - fee
        self.realized += pnl
        self.strategy_pnl[pos.strategy] = self.strategy_pnl.get(pos.strategy, 0.0) + pnl
        pos.qty -= qty
        pos.scaled = True
        if pnl >= 0:
            self.wins += 1
        else:
            self.losses += 1
        fill = Fill(
            id=uuid.uuid4().hex[:12],
            symbol=symbol,
            side=Side.SELL,
            qty=qty,
            price=px,
            fee=fee,
            ts=time.time(),
            strategy=pos.strategy,
            exchange=pos.exchange,
            paper=self.mode != "live",
            pnl=pnl,
            reason=reason,
        )
        await self.store.add_fill(fill)
        await self.emit("fill", {**fill.to_dict(), "pnl": pnl, "partial": True})

    def _compute_regime(self) -> dict[str, Any]:
        win = self.hub.candles.get("BTC/USDT")
        if not win or len(win) < 30:
            return {"name": "unknown", "risk_on": True, "detail": "warming up"}
        closes = list(win.closes)
        e9 = float(ema(closes, 9)[-1])
        e21 = float(ema(closes, 21)[-1])
        r = roc(closes, 12)
        rv = float(r[-1]) if len(r) and r[-1] == r[-1] else 0.0
        risk_on = e9 >= e21 and rv > -0.5
        return {
            "name": "risk_on" if risk_on else "risk_off",
            "risk_on": risk_on,
            "btc_ema9": e9,
            "btc_ema21": e21,
            "btc_roc": rv,
            "detail": "BTC trend up" if risk_on else "BTC defensive — size cut 45%",
        }

    def toggle_strategy(self, name: str, enabled: bool | None = None) -> bool | None:
        for s in self.strategies:
            if s.name == name:
                s.enabled = (not s.enabled) if enabled is None else bool(enabled)
                return s.enabled
        return None

    async def flatten(self) -> None:
        for sym in list(self.positions.keys()):
            t = self.hub.quote(sym)
            px = t.last if t else self.positions[sym].entry
            await self._close(sym, px, "manual flatten")

    async def set_watchlist(self, symbols: list[str]) -> None:
        cleaned = []
        seen = set()
        for s in symbols:
            s = s.strip().upper().replace("-", "/")
            if "/" not in s:
                s = f"{s}/{self.settings.quote_asset}"
            if s not in seen:
                seen.add(s)
                cleaned.append(s)
        self.watchlist = cleaned[: self.settings.max_watch_symbols]
        for sym in self.watchlist:
            if len(self.hub.candles[sym]) < 50:
                try:
                    kl = await fetch_klines(sym, self.settings.candle_interval, 180)
                    win = self.hub.candles[sym]
                    for c in kl:
                        win.push(c.ts, c.open, c.high, c.low, c.close, c.volume)
                except Exception as exc:
                    log.warning("watchlist kline %s: %s", sym, exc)
        await self.hub.start(self.watchlist)
        await self.emit("watchlist", {"symbols": self.watchlist})

    def snapshot(self) -> dict[str, Any]:
        equity = self.mark_equity
        peak = self.risk.peak_equity or equity
        dd = (peak - equity) / peak if peak else 0
        trades = self.wins + self.losses
        win_rate = self.wins / trades if trades else 0
        pos = []
        for p in self.positions.values():
            d = p.to_dict()
            d["unrealized"] = self._mtm(p)
            t = self.hub.quote(p.symbol)
            d["mark"] = t.last if t else p.entry
            pos.append(d)
        live_ready = has_live_keys()
        return {
            "mode": self.mode,
            "live_keys_present": live_ready,
            "running": self.running,
            "paused": self.paused,
            "halted": self.risk.halted,
            "halt_reason": self.risk.halt_reason,
            "started_at": self.started_at,
            "uptime": time.time() - self.started_at if self.started_at else 0,
            "loops": self.loops,
            "cash": self.cash,
            "equity": equity,
            "realized": self.realized,
            "unrealized": self.unrealized,
            "exposure": self.exposure,
            "starting_equity": self.settings.starting_equity,
            "drawdown": dd,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": win_rate,
            "positions": pos,
            "watchlist": self.watchlist,
            "strategies": [
                {
                    "name": s.name,
                    "title": getattr(s, "title", s.name),
                    "family": getattr(s, "family", "core"),
                    "weight": s.weight,
                    "enabled": s.enabled,
                    "pnl": self.strategy_pnl.get(s.name, 0.0),
                }
                for s in self.strategies
            ],
            "risk": self.settings.risk.model_dump(),
            "feeds": self.hub.health(),
            "rest_ok": self.rest_ok,
            "universe_size": len(self.universe),
            "regime": self.regime,
            "alerts": self.alerts[:12],
            "screener_n": (self.screener or {}).get("n", 0),
            "top_alpha": ((self.screener or {}).get("boards") or {}).get("alpha", [])[:3],
        }
