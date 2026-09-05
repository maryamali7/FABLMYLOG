"""Multi-timeframe engine.

Builds 1m / 5m / 15m / 1h / 4h / 1d / 1w views of every watched symbol so the
terminal can answer "is RSI overbought on the 15m while the 1h is still
trending up?".

Candles come from exchange REST history when reachable, and are otherwise
resampled from the live 1m rolling window, so the feature degrades gracefully
instead of disappearing.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any, Iterable

import numpy as np

from app.indicators import RollingWindow, clamp
from app.rules import compute_frame, context_at

log = logging.getLogger("mtf")

TF_ORDER = ["1m", "5m", "15m", "1h", "4h", "1d", "1w"]
TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14_400, "1d": 86_400, "1w": 604_800}
TF_LABEL = {"1m": "1 min", "5m": "5 min", "15m": "15 min", "1h": "1 hour", "4h": "4 hour", "1d": "1 day", "1w": "1 week"}
# how long a cached candle set stays fresh
TF_TTL = {"1m": 25, "5m": 60, "15m": 150, "1h": 420, "4h": 1200, "1d": 2400, "1w": 4800}
# higher timeframes carry more weight in the alignment score
TF_WEIGHT = {"1m": 0.55, "5m": 0.85, "15m": 1.05, "1h": 1.35, "4h": 1.5, "1d": 1.7, "1w": 1.25}
TF_LIMIT = {"1m": 400, "5m": 320, "15m": 320, "1h": 320, "4h": 260, "1d": 260, "1w": 160}

MIN_TF_BARS = 22

RSI_STATES = (
    (70.0, "overbought"),
    (58.0, "bullish"),
    (42.0, "neutral"),
    (30.0, "bearish"),
    (0.0, "oversold"),
)


def rsi_state(value: float) -> str:
    for threshold, label in RSI_STATES:
        if value >= threshold:
            return label
    return "oversold"


def rating_label(score: float) -> str:
    if score >= 45:
        return "strong buy"
    if score >= 18:
        return "buy"
    if score <= -45:
        return "strong sell"
    if score <= -18:
        return "sell"
    return "neutral"


def resample(candles: Any, seconds: int) -> list[dict[str, float]]:
    """Aggregate 1m candles (RollingWindow or rows) into a higher timeframe."""
    if isinstance(candles, RollingWindow):
        rows = [
            {
                "ts": candles.ts[i],
                "open": candles.opens[i],
                "high": candles.highs[i],
                "low": candles.lows[i],
                "close": candles.closes[i],
                "volume": candles.volumes[i],
            }
            for i in range(len(candles))
        ]
    else:
        rows = [
            r
            if isinstance(r, dict)
            else {
                "ts": r.ts,
                "open": r.open,
                "high": r.high,
                "low": r.low,
                "close": r.close,
                "volume": r.volume,
            }
            for r in candles
        ]
    if not rows or seconds <= 60:
        return rows
    out: list[dict[str, float]] = []
    bucket_ts = -1.0
    for r in rows:
        b = math.floor(float(r["ts"]) / seconds) * seconds
        if b != bucket_ts:
            out.append(
                {
                    "ts": b,
                    "open": float(r["open"]),
                    "high": float(r["high"]),
                    "low": float(r["low"]),
                    "close": float(r["close"]),
                    "volume": float(r.get("volume", 0) or 0),
                }
            )
            bucket_ts = b
        else:
            c = out[-1]
            c["high"] = max(c["high"], float(r["high"]))
            c["low"] = min(c["low"], float(r["low"]))
            c["close"] = float(r["close"])
            c["volume"] += float(r.get("volume", 0) or 0)
    return out


def tf_metrics(tf: str, candles: list[dict[str, float]], source: str = "rest") -> dict[str, Any]:
    """Indicator read-out + technical rating for a single timeframe."""
    n = len(candles or [])
    if n < MIN_TF_BARS:
        return {
            "tf": tf,
            "label": TF_LABEL.get(tf, tf),
            "available": False,
            "bars": n,
            "source": source,
            "reason": "not enough history yet",
        }
    frame = compute_frame(candles, max_bars=320)
    ctx = context_at(frame)
    g = lambda k, d=0.0: float(ctx.get(k, d))  # noqa: E731
    close = g("close")
    rsi = g("rsi", 50.0)
    adx = g("adx")
    pdi, mdi = g("plus_di"), g("minus_di")
    st_dir = 1 if g("st_dir", 1) >= 0 else -1
    stack = g("ema_stack")
    macd_hist = g("macd_hist")
    cloud = g("cloud_pos")

    score = 0.0
    score += clamp(g("ema9_dev") / 2.0, -1, 1) * 16
    score += 14 * (1 if stack > 0 else -1 if stack < 0 else 0)
    score += clamp((adx - 18) / 22.0, 0, 1) * 15 * (1 if pdi >= mdi else -1)
    score += st_dir * 12
    score += clamp(macd_hist / (abs(close) * 0.003 + 1e-9), -1, 1) * 12
    score += clamp((rsi - 50) / 25.0, -1, 1) * 10
    score += cloud * 8
    score += clamp((g("bb_pct", 0.5) - 0.5) * 2, -1, 1) * 5
    score += clamp(g("vwap_dev") / 1.5, -1, 1) * 5
    score = float(max(-100.0, min(100.0, score)))

    closes = frame["close"]
    change = float((closes[-1] / closes[-2] - 1) * 100) if len(closes) > 1 and closes[-2] else 0.0
    span = min(len(closes) - 1, 14)
    change_span = float((closes[-1] / closes[-1 - span] - 1) * 100) if span > 0 and closes[-1 - span] else 0.0

    return {
        "tf": tf,
        "label": TF_LABEL.get(tf, tf),
        "available": True,
        "bars": n,
        "source": source,
        "close": close,
        "change_pct": round(change, 3),
        "change_span_pct": round(change_span, 3),
        "rsi": round(rsi, 1),
        "rsi_state": rsi_state(rsi),
        "stoch_k": round(g("stoch_k"), 1),
        "srsi_k": round(g("srsi_k"), 1),
        "macd_hist": round(macd_hist, 8),
        "macd_state": "bullish" if macd_hist > 0 else "bearish",
        "adx": round(adx, 1),
        "plus_di": round(pdi, 1),
        "minus_di": round(mdi, 1),
        "ema_stack": int(stack),
        "supertrend": "bull" if st_dir == 1 else "bear",
        "cloud": "above" if cloud > 0 else ("below" if cloud < 0 else "inside"),
        "atr_pct": round(g("atr_pct"), 3),
        "bb_pct": round(g("bb_pct", 0.5), 3),
        "bb_width": round(g("bb_width"), 3),
        "vol_ratio": round(g("vol_ratio", 1.0), 2),
        "trend": "up" if score > 6 else ("down" if score < -6 else "flat"),
        "score": round(score, 1),
        "rating": rating_label(score),
        "squeeze": bool(g("squeeze")),
        "last_ts": float(frame["ts"][-1]) if len(frame.get("ts", [])) else 0.0,
    }


def align(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Blend per-timeframe ratings into one alignment verdict."""
    usable = [r for r in rows if r.get("available")]
    if not usable:
        return {
            "score": 0.0,
            "verdict": "unknown",
            "bias": "neutral",
            "agreement": 0.0,
            "bulls": 0,
            "bears": 0,
            "timeframes": 0,
            "detail": "waiting for higher-timeframe history",
        }
    total_w = sum(TF_WEIGHT.get(r["tf"], 1.0) for r in usable)
    score = sum(r["score"] * TF_WEIGHT.get(r["tf"], 1.0) for r in usable) / max(total_w, 1e-9)
    bulls = sum(1 for r in usable if r["score"] > 6)
    bears = sum(1 for r in usable if r["score"] < -6)
    dominant = max(bulls, bears)
    agreement = dominant / len(usable) * 100
    bias = "long" if score > 12 else ("short" if score < -12 else "neutral")
    if score >= 45:
        verdict = "strong uptrend"
    elif score >= 18:
        verdict = "uptrend"
    elif score <= -45:
        verdict = "strong downtrend"
    elif score <= -18:
        verdict = "downtrend"
    else:
        verdict = "mixed / rangebound"
    conflicts = [
        f"{r['tf']} {r['rsi_state']}"
        for r in usable
        if r["rsi_state"] in ("overbought", "oversold")
    ]
    return {
        "score": round(float(score), 1),
        "verdict": verdict,
        "bias": bias,
        "agreement": round(agreement, 1),
        "bulls": bulls,
        "bears": bears,
        "timeframes": len(usable),
        "conflicts": conflicts,
        "detail": f"{dominant}/{len(usable)} timeframes {'bullish' if bulls >= bears else 'bearish'}",
    }


class MTFEngine:
    """Caches multi-timeframe candles + metrics for the watchlist."""

    def __init__(self, hub, fetch_klines=None, timeframes: list[str] | None = None):
        self.hub = hub
        self.timeframes = timeframes or TF_ORDER
        self._fetch = fetch_klines
        self.candles: dict[tuple[str, str], dict[str, Any]] = {}
        self.metrics: dict[tuple[str, str], dict[str, Any]] = {}
        self.updated: dict[str, float] = {}
        self.rest_ok = True
        self._cursor = 0

    # -- data ------------------------------------------------------------- #
    def _resampled(self, symbol: str, tf: str) -> list[dict[str, float]]:
        win = self.hub.candles.get(symbol) if self.hub else None
        if not win or len(win) < MIN_TF_BARS:
            return []
        return resample(win, TF_SECONDS[tf])

    async def refresh_symbol(self, symbol: str, timeframes: list[str] | None = None, force: bool = False) -> None:
        now = time.time()
        for tf in timeframes or self.timeframes:
            key = (symbol, tf)
            cached = self.candles.get(key)
            if not force and cached and now - cached["ts"] < TF_TTL.get(tf, 120):
                continue
            rows: list[dict[str, float]] = []
            source = "resample"
            if self._fetch is not None and self.rest_ok:
                try:
                    kl = await self._fetch(symbol, tf, TF_LIMIT.get(tf, 300))
                    rows = [
                        {"ts": c.ts, "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}
                        for c in kl
                    ]
                    source = "rest"
                except Exception as exc:
                    log.debug("mtf rest %s %s: %s", symbol, tf, exc)
            if not rows:
                rows = self._resampled(symbol, tf)
                source = "resample"
            if not rows:
                continue
            self.candles[key] = {"ts": now, "rows": rows, "source": source}
            self.metrics[key] = tf_metrics(tf, rows, source)
        self.updated[symbol] = now

    async def refresh_next(self, symbols: list[str], batch: int = 1) -> list[str]:
        """Round-robin refresher so REST load stays spread over the loop."""
        if not symbols:
            return []
        done = []
        for _ in range(max(1, batch)):
            sym = symbols[self._cursor % len(symbols)]
            self._cursor += 1
            await self.refresh_symbol(sym)
            done.append(sym)
        return done

    # -- reads ------------------------------------------------------------ #
    def rows(self, symbol: str) -> list[dict[str, Any]]:
        out = []
        for tf in self.timeframes:
            m = self.metrics.get((symbol, tf))
            if m:
                out.append(m)
            else:
                out.append(
                    {
                        "tf": tf,
                        "label": TF_LABEL.get(tf, tf),
                        "available": False,
                        "bars": 0,
                        "source": "pending",
                        "reason": "not loaded yet",
                    }
                )
        return out

    def snapshot(self, symbol: str) -> dict[str, Any]:
        rows = self.rows(symbol)
        summary = align(rows)
        overbought = [r["tf"] for r in rows if r.get("rsi_state") == "overbought"]
        oversold = [r["tf"] for r in rows if r.get("rsi_state") == "oversold"]
        return {
            "symbol": symbol,
            "ts": self.updated.get(symbol, 0.0),
            "timeframes": rows,
            "alignment": summary,
            "overbought": overbought,
            "oversold": oversold,
            "ready": summary["timeframes"] > 0,
        }

    def flat_fields(self, symbol: str) -> dict[str, Any]:
        """Screener/rule-engine friendly flattened fields (rsi_1h, trend_4h, …)."""
        out: dict[str, Any] = {}
        rows = [r for r in self.rows(symbol) if r.get("available")]
        for r in rows:
            tf = r["tf"]
            out[f"rsi_{tf}"] = r["rsi"]
            out[f"score_{tf}"] = r["score"]
            out[f"trend_{tf}"] = r["trend"]
            out[f"adx_{tf}"] = r["adx"]
        summary = align(rows)
        out["mtf_score"] = summary["score"]
        out["mtf_agreement"] = summary["agreement"]
        out["mtf_bias"] = summary["bias"]
        out["mtf_verdict"] = summary["verdict"]
        out["mtf_timeframes"] = summary["timeframes"]
        out["mtf_overbought"] = sum(1 for r in rows if r.get("rsi_state") == "overbought")
        out["mtf_oversold"] = sum(1 for r in rows if r.get("rsi_state") == "oversold")
        return out

    def best_frame(self, symbol: str, tf: str = "1m") -> list[dict[str, float]]:
        entry = self.candles.get((symbol, tf))
        if entry:
            return entry["rows"]
        return self._resampled(symbol, tf) if tf != "1m" else []

    def scan(self, symbols: list[str]) -> list[dict[str, Any]]:
        """Alignment table across the watchlist (drives the MTF screener board)."""
        out = []
        for sym in symbols:
            rows = [r for r in self.rows(sym) if r.get("available")]
            if not rows:
                continue
            summary = align(rows)
            row = {
                "symbol": sym,
                "mtf_score": summary["score"],
                "agreement": summary["agreement"],
                "verdict": summary["verdict"],
                "bias": summary["bias"],
                "timeframes": summary["timeframes"],
                "bulls": summary["bulls"],
                "bears": summary["bears"],
            }
            for r in rows:
                row[f"rsi_{r['tf']}"] = r["rsi"]
                row[f"state_{r['tf']}"] = r["rsi_state"]
                row[f"trend_{r['tf']}"] = r["trend"]
            out.append(row)
        out.sort(key=lambda r: abs(r["mtf_score"]), reverse=True)
        return out
