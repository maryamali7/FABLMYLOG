"""Performance analytics over the trade journal.

Turns raw fills + the equity series into the numbers a desk actually reviews:
per-strategy edge, per-symbol edge, streaks, hourly distribution, drawdown and
risk-adjusted returns.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import numpy as np


def _stats(pnls: list[float]) -> dict[str, Any]:
    if not pnls:
        return {
            "trades": 0,
            "net": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "expectancy": 0.0,
            "best": 0.0,
            "worst": 0.0,
        }
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    wr = len(wins) / len(pnls)
    avg_win = gross_win / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0
    return {
        "trades": len(pnls),
        "net": round(sum(pnls), 2),
        "win_rate": round(wr * 100, 2),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else (99.0 if gross_win else 0.0),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "expectancy": round(wr * avg_win - (1 - wr) * avg_loss, 3),
        "best": round(max(pnls), 2),
        "worst": round(min(pnls), 2),
    }


def _streaks(pnls: list[float]) -> dict[str, int]:
    best = cur = worst = curl = 0
    for p in pnls:
        if p > 0:
            cur += 1
            curl = 0
            best = max(best, cur)
        else:
            curl += 1
            cur = 0
            worst = max(worst, curl)
    return {"longest_win_streak": best, "longest_loss_streak": worst, "current_streak": cur or -curl}


def equity_stats(series: list[dict[str, Any]]) -> dict[str, Any]:
    if len(series) < 3:
        return {"points": len(series)}
    eq = np.asarray([float(r.get("equity", 0) or 0) for r in series])
    ts = np.asarray([float(r.get("ts", 0) or 0) for r in series])
    peaks = np.maximum.accumulate(eq)
    dd = np.where(peaks > 0, (peaks - eq) / peaks, 0.0)
    rets = np.diff(eq) / np.clip(eq[:-1], 1e-9, None)
    span_days = max((ts[-1] - ts[0]) / 86400.0, 1e-6)
    periods_per_year = len(rets) / span_days * 365 if span_days else 0
    ann = math.sqrt(max(periods_per_year, 1))
    downside = rets[rets < 0]
    total_ret = (eq[-1] / eq[0] - 1) if eq[0] else 0.0
    return {
        "points": len(eq),
        "start_equity": round(float(eq[0]), 2),
        "end_equity": round(float(eq[-1]), 2),
        "return_pct": round(total_ret * 100, 3),
        "max_drawdown_pct": round(float(dd.max()) * 100, 3),
        "current_drawdown_pct": round(float(dd[-1]) * 100, 3),
        "volatility_pct": round(float(np.std(rets)) * 100, 4),
        "sharpe": round(float(np.mean(rets) / np.std(rets) * ann), 3)
        if len(rets) > 2 and np.std(rets) > 0
        else 0.0,
        "sortino": round(float(np.mean(rets) / np.std(downside) * ann), 3)
        if len(downside) > 2 and np.std(downside) > 0
        else 0.0,
        "calmar": round(total_ret / float(dd.max()), 3) if dd.max() > 0 else 0.0,
        "span_hours": round((ts[-1] - ts[0]) / 3600, 2),
    }


def analyze(fills: list[dict[str, Any]], equity: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Full analytics payload for the dashboard."""
    closed = [f for f in fills if f.get("side") == "sell" or float(f.get("pnl") or 0) != 0]
    pnls = [float(f.get("pnl") or 0) for f in closed]
    by_strategy: dict[str, list[float]] = defaultdict(list)
    by_symbol: dict[str, list[float]] = defaultdict(list)
    by_reason: dict[str, list[float]] = defaultdict(list)
    by_hour: dict[int, list[float]] = defaultdict(list)
    by_day: dict[str, list[float]] = defaultdict(list)
    for f in closed:
        pnl = float(f.get("pnl") or 0)
        by_strategy[f.get("strategy") or "unknown"].append(pnl)
        by_symbol[f.get("symbol") or "?"].append(pnl)
        by_reason[(f.get("reason") or "signal").split(":")[0][:28]].append(pnl)
        ts = float(f.get("ts") or 0)
        if ts:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            by_hour[dt.hour].append(pnl)
            by_day[dt.strftime("%Y-%m-%d")].append(pnl)

    def table(d: dict[Any, list[float]], key: str) -> list[dict[str, Any]]:
        rows = [{key: k, **_stats(v)} for k, v in d.items()]
        rows.sort(key=lambda r: r["net"], reverse=True)
        return rows

    fee_total = round(sum(float(f.get("fee") or 0) for f in fills), 4)
    volume = round(sum(float(f.get("qty") or 0) * float(f.get("price") or 0) for f in fills), 2)
    return {
        "ts": time.time(),
        "overall": {**_stats(pnls), **_streaks(pnls)},
        "by_strategy": table(by_strategy, "strategy"),
        "by_symbol": table(by_symbol, "symbol")[:25],
        "by_reason": table(by_reason, "reason"),
        "by_hour": [
            {"hour": h, "net": round(sum(v), 2), "trades": len(v)} for h, v in sorted(by_hour.items())
        ],
        "by_day": [
            {"day": d, "net": round(sum(v), 2), "trades": len(v)} for d, v in sorted(by_day.items())
        ][-30:],
        "equity": equity_stats(equity or []),
        "fees_paid": fee_total,
        "gross_volume": volume,
        "pnl_histogram": _histogram(pnls),
    }


def _histogram(pnls: list[float], buckets: int = 12) -> list[dict[str, Any]]:
    if not pnls:
        return []
    lo, hi = min(pnls), max(pnls)
    if math.isclose(lo, hi):
        return [{"from": round(lo, 2), "to": round(hi, 2), "count": len(pnls)}]
    edges = np.linspace(lo, hi, buckets + 1)
    counts, _ = np.histogram(pnls, bins=edges)
    return [
        {"from": round(float(edges[i]), 2), "to": round(float(edges[i + 1]), 2), "count": int(counts[i])}
        for i in range(len(counts))
    ]
