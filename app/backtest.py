"""Bar-replay backtester for builder strategies and built-ins.

Fast enough to run interactively from the dashboard: indicators are computed
once for the whole series, then rules are replayed bar by bar with fees,
slippage, stops, targets and trailing exits.
"""

from __future__ import annotations

import math
import time
from typing import Any

import numpy as np

from app.custom import CustomStrategy, normalize_spec
from app.indicators import RollingWindow
from app.models import SignalKind
from app.rules import FORECAST_FIELDS, MTF_FIELDS, compute_frame, context_at
from app.strategies import REGISTRY
from app.timeframes import MTFHistory, mtf_history

DEFAULTS = {
    "starting_equity": 10_000.0,
    "position_pct": 0.25,
    "fee_bps": 10.0,
    "slippage_bps": 5.0,
    "stop_loss_pct": 0.02,
    "take_profit_pct": 0.04,
    "trail_pct": 0.0,
    "max_bars_held": 240,
    "allow_short": False,
    "warmup": 60,
}


def _metrics(trades: list[dict[str, Any]], equity: list[float], bars: int, held: int) -> dict[str, Any]:
    if not equity:
        equity = [DEFAULTS["starting_equity"]]
    start, end = equity[0], equity[-1]
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    arr = np.asarray(equity, dtype=float)
    peaks = np.maximum.accumulate(arr)
    dd = np.where(peaks > 0, (peaks - arr) / peaks, 0.0)
    rets = np.diff(arr) / np.clip(arr[:-1], 1e-9, None) if len(arr) > 1 else np.zeros(1)
    downside = rets[rets < 0]
    ann = math.sqrt(365 * 24 * 60)  # 1m bars
    sharpe = float(np.mean(rets) / np.std(rets) * ann) if len(rets) > 2 and np.std(rets) > 0 else 0.0
    sortino = (
        float(np.mean(rets) / np.std(downside) * ann)
        if len(downside) > 2 and np.std(downside) > 0
        else 0.0
    )
    avg_win = gross_win / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0
    win_rate = len(wins) / len(trades) if trades else 0.0
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate * 100, 2),
        "net_pnl": round(end - start, 2),
        "return_pct": round((end / start - 1) * 100, 3) if start else 0.0,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else (99.0 if gross_win else 0.0),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "expectancy": round(win_rate * avg_win - (1 - win_rate) * avg_loss, 3),
        "payoff": round(avg_win / avg_loss, 3) if avg_loss else 0.0,
        "max_drawdown_pct": round(float(dd.max()) * 100, 3) if len(dd) else 0.0,
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "exposure_pct": round(held / bars * 100, 2) if bars else 0.0,
        "best_trade": round(max((t["pnl"] for t in trades), default=0.0), 2),
        "worst_trade": round(min((t["pnl"] for t in trades), default=0.0), 2),
        "avg_bars_held": round(sum(t["bars"] for t in trades) / len(trades), 1) if trades else 0.0,
        "final_equity": round(end, 2),
    }


def _grade(m: dict[str, Any]) -> str:
    score = 0
    score += 2 if m["profit_factor"] >= 1.6 else 1 if m["profit_factor"] >= 1.15 else 0
    score += 2 if m["win_rate"] >= 55 else 1 if m["win_rate"] >= 45 else 0
    score += 2 if m["return_pct"] > 2 else 1 if m["return_pct"] > 0 else 0
    score += 1 if m["max_drawdown_pct"] < 8 else 0
    score += 1 if m["trades"] >= 8 else 0
    return ["D", "D", "C", "C", "B", "B", "A", "A+"][min(score, 7)]


def _referenced_fields(node: Any, out: set[str] | None = None) -> set[str]:
    """Every field name a rule tree touches (left sides and field-valued rights)."""
    out = set() if out is None else out
    if isinstance(node, dict):
        for key in ("rules", "conditions", "any", "all", "none"):
            for child in node.get(key) or []:
                _referenced_fields(child, out)
        for side in ("left", "right"):
            val = node.get(side)
            if isinstance(val, str):
                out.add(val.strip())
        if isinstance(node.get("expr"), str):
            for token in node["expr"].replace("(", " ").replace(")", " ").split():
                out.add(token.strip())
    elif isinstance(node, list):
        for child in node:
            _referenced_fields(child, out)
    return out


def spec_fields(spec: dict[str, Any]) -> set[str]:
    fields: set[str] = set()
    for key in ("entry", "exit", "short_entry"):
        _referenced_fields(spec.get(key), fields)
    return fields


def backtest(
    candles: Any,
    spec: dict[str, Any] | None = None,
    builtin: str | None = None,
    symbol: str = "BACKTEST",
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replay a strategy over candles.

    ``spec`` runs a builder strategy, ``builtin`` runs a registered strategy by
    name (e.g. ``"macd_trend"``).
    """
    cfg = {**DEFAULTS, **(config or {})}
    t0 = time.time()
    frame = compute_frame(candles, max_bars=int(cfg.get("max_bars", 1500)))
    if not frame or len(frame.get("close", [])) < 60:
        return {"ok": False, "error": "not enough candles to backtest (need 60+)", "metrics": {}}

    closes = frame["close"]
    highs = frame["high"]
    lows = frame["low"]
    ts = frame.get("ts", np.arange(len(closes), dtype=float))
    n = len(closes)

    strat: Any = None
    if spec is not None:
        strat = CustomStrategy({**normalize_spec(spec), "cooldown_sec": 0})
        stop_pct = spec.get("stop_loss_pct") or cfg["stop_loss_pct"]
        take_pct = spec.get("take_profit_pct") or cfg["take_profit_pct"]
        trail_pct = spec.get("trail_pct") or cfg["trail_pct"]
        allow_short = spec.get("side") in ("short", "both") or cfg["allow_short"]
    elif builtin:
        cls = REGISTRY.get(builtin)
        if not cls:
            return {"ok": False, "error": f"unknown strategy '{builtin}'", "metrics": {}}
        strat = cls({"enabled": True, "weight": 1.0})
        stop_pct, take_pct = cfg["stop_loss_pct"], cfg["take_profit_pct"]
        trail_pct = cfg["trail_pct"]
        allow_short = bool(cfg["allow_short"])
    else:
        return {"ok": False, "error": "provide a strategy spec or builtin name", "metrics": {}}

    notes: list[str] = []
    hist: MTFHistory | None = None
    if spec is not None:
        used = spec_fields(spec)
        if used & set(MTF_FIELDS):
            try:
                hist = mtf_history(candles)
            except Exception as exc:  # pragma: no cover - defensive
                notes.append(f"multi-timeframe context unavailable: {exc}")
            if hist is not None:
                if hist.available:
                    notes.append(
                        "multi-timeframe context replayed on closed bars only: "
                        + ", ".join(hist.available)
                    )
                else:
                    notes.append(
                        "not enough history to rebuild higher timeframes — "
                        "timeframe rules were skipped"
                    )
        missing_fc = sorted(used & set(FORECAST_FIELDS))
        if missing_fc:
            notes.append(
                "forecast fields are live-only and are not simulated in replay: "
                + ", ".join(missing_fc)
            )

    fee = float(cfg["fee_bps"]) / 10_000.0
    slip = float(cfg["slippage_bps"]) / 10_000.0
    equity = float(cfg["starting_equity"])
    curve: list[float] = []
    curve_ts: list[float] = []
    trades: list[dict[str, Any]] = []
    warmup = max(25, min(int(cfg["warmup"]), n // 3))
    pos: dict[str, Any] | None = None
    bars_held = 0

    # rolling window only needed for built-in strategies
    win = RollingWindow(1600) if builtin else None
    if win is not None:
        for i in range(min(warmup, n)):
            win.push(ts[i], frame["open"][i], highs[i], lows[i], closes[i], frame["volume"][i])

    for i in range(warmup, n):
        px = float(closes[i])
        if win is not None:
            win.push(ts[i], frame["open"][i], highs[i], lows[i], px, frame["volume"][i])

        # ---- manage open position first
        if pos:
            bars_held += 1
            pos["bars"] += 1
            side = pos["side"]
            hi, lo = float(highs[i]), float(lows[i])
            if side == "long":
                pos["peak"] = max(pos["peak"], hi)
            else:
                pos["peak"] = min(pos["peak"], lo)
            exit_px: float | None = None
            reason = ""
            if side == "long":
                if stop_pct and lo <= pos["entry"] * (1 - stop_pct):
                    exit_px, reason = pos["entry"] * (1 - stop_pct), "stop"
                elif take_pct and hi >= pos["entry"] * (1 + take_pct):
                    exit_px, reason = pos["entry"] * (1 + take_pct), "target"
                elif trail_pct and pos["peak"] > pos["entry"] and lo <= pos["peak"] * (1 - trail_pct):
                    exit_px, reason = pos["peak"] * (1 - trail_pct), "trail"
            else:
                if stop_pct and hi >= pos["entry"] * (1 + stop_pct):
                    exit_px, reason = pos["entry"] * (1 + stop_pct), "stop"
                elif take_pct and lo <= pos["entry"] * (1 - take_pct):
                    exit_px, reason = pos["entry"] * (1 - take_pct), "target"
                elif trail_pct and pos["peak"] < pos["entry"] and hi >= pos["peak"] * (1 + trail_pct):
                    exit_px, reason = pos["peak"] * (1 + trail_pct), "trail"
            if exit_px is None and pos["bars"] >= int(cfg["max_bars_held"]):
                exit_px, reason = px, "time stop"
            if exit_px is None:
                sig = _signal_at(strat, symbol, frame, i, px, win, hist, float(ts[i]))
                if sig is not None:
                    want_exit = (side == "long" and sig.kind == SignalKind.SELL) or (
                        side == "short" and sig.kind == SignalKind.BUY
                    )
                    if want_exit:
                        exit_px, reason = px, "signal exit"
            if exit_px is not None:
                fill = exit_px * (1 - slip) if side == "long" else exit_px * (1 + slip)
                gross = (fill - pos["entry"]) * pos["qty"]
                if side == "short":
                    gross = (pos["entry"] - fill) * pos["qty"]
                fees = (pos["entry"] + fill) * pos["qty"] * fee
                pnl = gross - fees
                equity += pnl
                trades.append(
                    {
                        "side": side,
                        "entry": round(pos["entry"], 8),
                        "exit": round(fill, 8),
                        "qty": pos["qty"],
                        "pnl": round(pnl, 4),
                        "pnl_pct": round(pnl / max(1e-9, pos["notional"]) * 100, 3),
                        "bars": pos["bars"],
                        "reason": reason,
                        "entry_ts": pos["ts"],
                        "exit_ts": float(ts[i]) if i < len(ts) else 0.0,
                    }
                )
                pos = None

        # ---- look for a new entry
        if pos is None:
            sig = _signal_at(strat, symbol, frame, i, px, win, hist, float(ts[i]))
            if sig is not None and sig.kind in (SignalKind.BUY, SignalKind.SELL):
                side = "long" if sig.kind == SignalKind.BUY else "short"
                if side == "short" and not allow_short:
                    sig = None
                else:
                    notional = equity * float(cfg["position_pct"])
                    entry = px * (1 + slip) if side == "long" else px * (1 - slip)
                    qty = notional / max(entry, 1e-9)
                    pos = {
                        "side": side,
                        "entry": entry,
                        "qty": qty,
                        "notional": notional,
                        "bars": 0,
                        "peak": entry,
                        "ts": float(ts[i]) if i < len(ts) else 0.0,
                    }

        mark = equity
        if pos:
            if pos["side"] == "long":
                mark = equity + (px - pos["entry"]) * pos["qty"]
            else:
                mark = equity + (pos["entry"] - px) * pos["qty"]
        curve.append(round(mark, 4))
        curve_ts.append(float(ts[i]) if i < len(ts) else float(i))

    metrics = _metrics(trades, curve, max(1, n - warmup), bars_held)
    metrics["grade"] = _grade(metrics)
    step = max(1, len(curve) // 240)
    return {
        "ok": True,
        "symbol": symbol,
        "strategy": (spec or {}).get("name") or builtin,
        "bars": n,
        "from_ts": float(ts[warmup]) if n > warmup else 0.0,
        "to_ts": float(ts[-1]) if n else 0.0,
        "metrics": metrics,
        "trades": trades[-60:],
        "equity_curve": curve[::step],
        "equity_ts": curve_ts[::step],
        "buy_hold_pct": round((float(closes[-1]) / float(closes[warmup]) - 1) * 100, 3)
        if n > warmup and closes[warmup]
        else 0.0,
        "elapsed_ms": round((time.time() - t0) * 1000, 1),
        "config": cfg,
        "notes": notes,
        "timeframes": list(hist.available) if hist is not None else [],
    }


def _signal_at(
    strat: Any,
    symbol: str,
    frame: dict[str, np.ndarray],
    i: int,
    px: float,
    win: RollingWindow | None,
    hist: MTFHistory | None = None,
    ts: float | None = None,
):
    try:
        if isinstance(strat, CustomStrategy):
            extra: dict[str, Any] = {"live_price": px}
            if hist is not None and ts is not None:
                extra.update(hist.fields_at(ts))
            ctx = context_at(frame, i, extra=extra)
            return strat.evaluate_ctx(symbol, ctx, px)
        if win is not None:
            return strat.evaluate(symbol, win, px)
    except Exception:
        return None
    return None


def compare(candles: Any, specs: list[dict[str, Any]], symbol: str, config: dict[str, Any] | None = None):
    """Backtest several strategies over the same candles and rank them."""
    out = []
    for spec in specs:
        if isinstance(spec, str):
            res = backtest(candles, builtin=spec, symbol=symbol, config=config)
            label = spec
        else:
            res = backtest(candles, spec=spec, symbol=symbol, config=config)
            label = spec.get("name", "custom")
        if res.get("ok"):
            out.append({"name": label, **res["metrics"]})
    out.sort(key=lambda r: (r.get("return_pct", 0), r.get("profit_factor", 0)), reverse=True)
    return out


def portfolio_backtest(
    series: dict[str, Any],
    spec: dict[str, Any] | None = None,
    builtin: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the same strategy across several symbols and aggregate the results."""
    per_symbol = []
    for sym, candles in series.items():
        res = backtest(candles, spec=spec, builtin=builtin, symbol=sym, config=config)
        if res.get("ok"):
            per_symbol.append({"symbol": sym, **res["metrics"]})
    if not per_symbol:
        return {"ok": False, "error": "no symbol produced a valid backtest", "symbols": []}
    agg_trades = sum(r["trades"] for r in per_symbol)
    agg_pnl = sum(r["net_pnl"] for r in per_symbol)
    wins = sum(r["wins"] for r in per_symbol)
    per_symbol.sort(key=lambda r: r["return_pct"], reverse=True)
    return {
        "ok": True,
        "symbols": per_symbol,
        "totals": {
            "symbols": len(per_symbol),
            "trades": agg_trades,
            "net_pnl": round(agg_pnl, 2),
            "win_rate": round(wins / agg_trades * 100, 2) if agg_trades else 0.0,
            "avg_return_pct": round(sum(r["return_pct"] for r in per_symbol) / len(per_symbol), 3),
            "best": per_symbol[0]["symbol"],
            "worst": per_symbol[-1]["symbol"],
        },
    }
