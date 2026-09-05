"""Next-move forecasting.

A transparent ensemble of six models — trend, mean-reversion, historical analogs
(k-NN pattern matching), drift regression, order-flow and volatility — that
produces a directional probability, an expected move, a projected price cone,
support/resistance levels and a plain-English rationale.

Nothing here is a black box: every model exposes its own score, weight and
confidence so the dashboard can show *why* the robot leans a certain way.
"""

from __future__ import annotations

import math
import time
from typing import Any

import numpy as np

from app.indicators import clamp
from app.rules import compute_frame, context_at

HORIZONS = {"1m": 15, "5m": 12, "15m": 8, "1h": 6, "4h": 5, "1d": 4, "1w": 3}


def _sig(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-clamp(x, -12, 12)))


def _pct(logret: float) -> float:
    return float((math.exp(clamp(logret, -1.5, 1.5)) - 1.0) * 100.0)


# --------------------------------------------------------------------------- #
# structure
# --------------------------------------------------------------------------- #


def levels(frame: dict[str, np.ndarray], max_levels: int = 5) -> dict[str, Any]:
    """Swing-pivot support / resistance clustered by proximity and touches."""
    highs, lows, closes = frame["high"], frame["low"], frame["close"]
    vols = frame.get("volume", np.ones(len(closes)))
    n = len(closes)
    if n < 20:
        return {"support": [], "resistance": [], "nearest_support": None, "nearest_resistance": None}
    px = float(closes[-1])
    pivots: list[tuple[float, float, str]] = []
    span = 2
    for i in range(span, n - span):
        window_h = highs[i - span : i + span + 1]
        window_l = lows[i - span : i + span + 1]
        weight = 1.0 + float(vols[i]) / (float(np.mean(vols)) + 1e-9) * 0.4
        if highs[i] >= window_h.max():
            pivots.append((float(highs[i]), weight, "res"))
        if lows[i] <= window_l.min():
            pivots.append((float(lows[i]), weight, "sup"))
    # cluster pivots within 0.45%
    clusters: list[dict[str, Any]] = []
    for price, weight, kind in sorted(pivots):
        placed = False
        for c in clusters:
            if abs(price - c["price"]) / max(c["price"], 1e-9) < 0.0045:
                c["price"] = (c["price"] * c["weight"] + price * weight) / (c["weight"] + weight)
                c["weight"] += weight
                c["touches"] += 1
                placed = True
                break
        if not placed:
            clusters.append({"price": price, "weight": weight, "touches": 1, "kind": kind})
    for c in clusters:
        c["strength"] = round(min(100.0, c["touches"] * 18 + c["weight"] * 6), 1)
        c["distance_pct"] = round((c["price"] - px) / px * 100, 3)
        c["price"] = round(c["price"], 10)
        c.pop("weight", None)
    resistance = sorted([c for c in clusters if c["price"] > px], key=lambda c: c["price"])[:max_levels]
    support = sorted([c for c in clusters if c["price"] <= px], key=lambda c: -c["price"])[:max_levels]
    return {
        "price": px,
        "support": support,
        "resistance": resistance,
        "nearest_support": support[0] if support else None,
        "nearest_resistance": resistance[0] if resistance else None,
    }


def regime(ctx: dict[str, Any]) -> dict[str, Any]:
    adx = float(ctx.get("adx", 0))
    bb_width = float(ctx.get("bb_width", 0))
    atr_pct = float(ctx.get("atr_pct", 0))
    squeeze = bool(float(ctx.get("squeeze", 0)))
    if squeeze:
        name, detail = "compression", "Volatility squeezed — expect an expansion move"
    elif adx >= 28:
        name, detail = "trending", "Directional regime, pullbacks favour continuation"
    elif adx <= 15 and bb_width < 2.5:
        name, detail = "ranging", "No trend — fade the edges of the range"
    elif atr_pct > 1.2:
        name, detail = "volatile", "Wide ranges — size down and widen stops"
    else:
        name, detail = "transitional", "Mixed conditions, wait for confirmation"
    return {"name": name, "detail": detail, "adx": round(adx, 1), "atr_pct": round(atr_pct, 3)}


# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #


def trend_model(ctx: dict[str, Any], mtf_score: float | None) -> dict[str, Any]:
    stack = float(ctx.get("ema_stack", 0))
    adx = float(ctx.get("adx", 0))
    st = 1.0 if float(ctx.get("st_dir", 1)) >= 0 else -1.0
    di = 1.0 if float(ctx.get("plus_di", 0)) >= float(ctx.get("minus_di", 0)) else -1.0
    dev = clamp(float(ctx.get("ema9_dev", 0)) / 1.5, -1, 1)
    score = 0.30 * stack + 0.22 * st + 0.18 * di * clamp((adx - 15) / 25, 0, 1) + 0.18 * dev
    score += 0.12 * clamp(float(ctx.get("cloud_pos", 0)), -1, 1)
    conf = clamp(0.35 + (adx / 60.0), 0, 1)
    if mtf_score is not None:
        score = score * 0.65 + clamp(mtf_score / 100.0, -1, 1) * 0.35
        conf = clamp(conf + 0.15, 0, 1)
    return {
        "name": "Trend",
        "score": round(float(clamp(score, -1, 1)), 3),
        "confidence": round(float(conf), 3),
        "weight": 1.25,
        "detail": f"ADX {adx:.0f}, supertrend {'bull' if st > 0 else 'bear'}, EMA stack {int(stack)}",
    }


def reversion_model(ctx: dict[str, Any]) -> dict[str, Any]:
    z = float(ctx.get("zscore", 0))
    rsi = float(ctx.get("rsi", 50))
    bb = float(ctx.get("bb_pct", 0.5))
    stretch = clamp(-z / 2.4, -1, 1) * 0.5 + clamp((50 - rsi) / 28.0, -1, 1) * 0.3 + clamp((0.5 - bb) * 2.2, -1, 1) * 0.2
    conf = clamp((abs(z) - 0.8) / 2.0, 0, 1) * clamp(1.2 - float(ctx.get("adx", 0)) / 40.0, 0.15, 1)
    return {
        "name": "Mean reversion",
        "score": round(float(clamp(stretch, -1, 1)), 3),
        "confidence": round(float(conf), 3),
        "weight": 1.0,
        "detail": f"z {z:+.2f}, RSI {rsi:.0f}, %B {bb:.2f}",
    }


def analog_model(closes: np.ndarray, window: int = 20, horizon: int = 12, k: int = 15) -> dict[str, Any]:
    """k-NN over past price shapes: what happened after similar patterns?"""
    n = len(closes)
    need = window + horizon + 45
    if n < need:
        return {
            "name": "Historical analogs",
            "score": 0.0,
            "confidence": 0.0,
            "weight": 1.35,
            "detail": f"needs {need} bars, has {n}",
            "expected_pct": 0.0,
        }
    rets = np.diff(np.log(np.clip(closes, 1e-12, None)))
    views = np.lib.stride_tricks.sliding_window_view(rets, window)
    usable = len(views) - horizon - 1
    if usable < 25:
        return {
            "name": "Historical analogs",
            "score": 0.0,
            "confidence": 0.0,
            "weight": 1.35,
            "detail": "not enough analogs",
            "expected_pct": 0.0,
        }
    hist = views[:usable]
    cur = views[-1]

    def unit(a: np.ndarray) -> np.ndarray:
        s = a.std(axis=-1, keepdims=True)
        return a / np.where(s > 1e-12, s, 1.0)

    dist = np.linalg.norm(unit(hist) - unit(cur), axis=1)
    k = int(min(k, max(5, usable // 6)))
    idx = np.argsort(dist)[:k]
    fwd = np.array([rets[i + window : i + window + horizon].sum() for i in idx])
    if not len(fwd):
        return {"name": "Historical analogs", "score": 0.0, "confidence": 0.0, "weight": 1.35, "detail": "no matches", "expected_pct": 0.0}
    median = float(np.median(fwd))
    up_ratio = float((fwd > 0).mean())
    dispersion = float(np.std(fwd))
    typical = float(np.std(rets)) * math.sqrt(horizon) + 1e-9
    score = clamp(median / (typical * 1.2), -1, 1) * 0.6 + (up_ratio - 0.5) * 2 * 0.4
    tightness = clamp(1.0 - float(np.mean(dist[idx if False else np.argsort(dist)[:k]])) / (math.sqrt(window) * 1.4), 0, 1)
    agreement = abs(up_ratio - 0.5) * 2
    conf = clamp(0.25 + 0.45 * agreement + 0.3 * tightness, 0, 1)
    if dispersion > typical * 2.2:
        conf *= 0.6
    return {
        "name": "Historical analogs",
        "score": round(float(clamp(score, -1, 1)), 3),
        "confidence": round(float(conf), 3),
        "weight": 1.35,
        "expected_pct": round(_pct(median), 3),
        "hit_rate": round(up_ratio * 100, 1),
        "matches": int(k),
        "detail": f"{k} similar patterns → {up_ratio * 100:.0f}% closed higher, median {_pct(median):+.2f}%",
    }


def drift_model(closes: np.ndarray, horizon: int, lookback: int = 60) -> dict[str, Any]:
    n = len(closes)
    if n < 25:
        return {"name": "Drift regression", "score": 0.0, "confidence": 0.0, "weight": 0.9, "detail": "warming up", "expected_pct": 0.0}
    y = np.log(np.clip(closes[-min(lookback, n) :], 1e-12, None))
    x = np.arange(len(y), dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    fit = slope * x + intercept
    ss_res = float(np.sum((y - fit) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2)) + 1e-12
    r2 = clamp(1 - ss_res / ss_tot, 0, 1)
    projected = float(slope) * horizon
    sigma = float(np.std(np.diff(y))) * math.sqrt(horizon) + 1e-9
    return {
        "name": "Drift regression",
        "score": round(float(clamp(projected / (sigma * 1.5), -1, 1)), 3),
        "confidence": round(float(clamp(r2, 0, 1)), 3),
        "weight": 0.9,
        "expected_pct": round(_pct(projected), 3),
        "r2": round(float(r2), 3),
        "detail": f"slope fit R² {r2:.2f} → {_pct(projected):+.2f}% over {horizon} bars",
    }


def flow_model(ctx: dict[str, Any], book: Any = None, ticker: Any = None) -> dict[str, Any]:
    obv = clamp(float(ctx.get("obv_slope", 0)) * 4, -1, 1)
    volz = clamp(float(ctx.get("vol_z", 0)) / 2.5, -1, 1)
    pressure = clamp((float(ctx.get("buy_pressure", 50)) - 50) / 35.0, -1, 1)
    vwap = clamp(float(ctx.get("vwap_dev", 0)) / 1.2, -1, 1)
    imbalance = 0.0
    depth_note = ""
    if book is not None and getattr(book, "bids", None) and getattr(book, "asks", None):
        bid_qty = sum(l.qty for l in book.bids[:10])
        ask_qty = sum(l.qty for l in book.asks[:10])
        if bid_qty + ask_qty > 0:
            imbalance = clamp((bid_qty - ask_qty) / (bid_qty + ask_qty), -1, 1)
            depth_note = f", book {imbalance:+.2f}"
    score = 0.3 * obv + 0.2 * volz * (1 if pressure >= 0 else -1) + 0.25 * pressure + 0.1 * vwap + 0.15 * imbalance
    conf = clamp(0.3 + abs(volz) * 0.35 + abs(imbalance) * 0.35, 0, 1)
    return {
        "name": "Order flow",
        "score": round(float(clamp(score, -1, 1)), 3),
        "confidence": round(float(conf), 3),
        "weight": 0.85,
        "detail": f"OBV {obv:+.2f}, pressure {pressure:+.2f}{depth_note}",
    }


def volatility_model(closes: np.ndarray, horizon: int, lam: float = 0.94) -> dict[str, Any]:
    if len(closes) < 12:
        return {"sigma_pct": 1.0, "expected_range_pct": 1.0, "annualized_pct": 0.0}
    rets = np.diff(np.log(np.clip(closes, 1e-12, None)))
    var = float(rets[0] ** 2)
    for r in rets[1:]:
        var = lam * var + (1 - lam) * float(r) ** 2
    sigma = math.sqrt(max(var, 1e-14))
    sigma_h = sigma * math.sqrt(horizon)
    return {
        "sigma_pct": round(sigma_h * 100, 4),
        "expected_range_pct": round(sigma_h * 100 * 2, 4),
        "annualized_pct": round(sigma * math.sqrt(365 * 24 * 60) * 100, 2),
    }


# --------------------------------------------------------------------------- #
# ensemble
# --------------------------------------------------------------------------- #


def predict(
    candles: Any,
    symbol: str = "",
    timeframe: str = "1m",
    horizon: int | None = None,
    mtf: dict[str, Any] | None = None,
    book: Any = None,
    ticker: Any = None,
) -> dict[str, Any]:
    started = time.time()
    frame = compute_frame(candles, max_bars=420)
    if not frame or len(frame.get("close", [])) < 40:
        return {"ok": False, "error": "not enough candles to forecast (need 40+)", "symbol": symbol}
    ctx = context_at(frame)
    closes = frame["close"]
    price = float(closes[-1])
    h = int(horizon or HORIZONS.get(timeframe, 12))

    mtf_score = None
    if mtf and mtf.get("alignment", {}).get("timeframes"):
        mtf_score = float(mtf["alignment"]["score"])

    models = [
        trend_model(ctx, mtf_score),
        reversion_model(ctx),
        analog_model(closes, horizon=h),
        drift_model(closes, h),
        flow_model(ctx, book, ticker),
    ]
    vol = volatility_model(closes, h)

    num = sum(m["score"] * m["weight"] * m["confidence"] for m in models)
    den = sum(m["weight"] * m["confidence"] for m in models)
    direction_score = float(num / den) if den > 1e-9 else 0.0

    # expected move: blend the models that produce an explicit return estimate
    est = [(m["expected_pct"], m["weight"] * m["confidence"]) for m in models if "expected_pct" in m and m["confidence"] > 0.05]
    if est:
        move = sum(v * w for v, w in est) / sum(w for _, w in est)
    else:
        move = direction_score * vol["sigma_pct"]
    # keep the projection inside a sane multiple of forecast volatility
    cap = vol["sigma_pct"] * 2.5
    move = float(clamp(move * 0.55 + direction_score * vol["sigma_pct"] * 0.45, -cap, cap))

    prob_up = clamp(0.5 + 0.44 * direction_score, 0.02, 0.98)
    analog = next((m for m in models if m["name"] == "Historical analogs"), None)
    if analog and analog.get("confidence", 0) > 0.35 and "hit_rate" in analog:
        prob_up = clamp(prob_up * 0.75 + (analog["hit_rate"] / 100.0) * 0.25, 0.02, 0.98)

    scores = np.array([m["score"] for m in models if m["confidence"] > 0.1]) if models else np.array([0.0])
    dispersion = float(np.std(scores)) if len(scores) > 1 else 1.0
    data_conf = clamp(len(closes) / 260.0, 0.3, 1.0)
    agreement = clamp(1.0 - dispersion, 0, 1)
    confidence = clamp(0.15 + 0.4 * agreement + 0.25 * abs(direction_score) + 0.2 * data_conf, 0, 0.97)
    if mtf and mtf.get("alignment", {}).get("agreement"):
        confidence = clamp(confidence * 0.85 + (mtf["alignment"]["agreement"] / 100.0) * 0.15, 0, 0.97)

    direction = "up" if direction_score > 0.08 else ("down" if direction_score < -0.08 else "flat")
    target = price * (1 + move / 100.0)
    band = vol["sigma_pct"] * 1.28  # ~80% interval
    struct = levels(frame)
    reg = regime(ctx)

    # projected path for the chart: drift to target with a widening cone
    path = []
    for i in range(1, h + 1):
        frac = i / h
        mid = price * (1 + (move / 100.0) * frac)
        spread = price * (band / 100.0) * math.sqrt(frac)
        path.append({"step": i, "mid": mid, "upper": mid + spread, "lower": mid - spread})

    rationale: list[str] = []
    for m in sorted(models, key=lambda x: -abs(x["score"]) * x["confidence"])[:3]:
        if m["confidence"] < 0.08:
            continue
        lean = "bullish" if m["score"] > 0 else "bearish" if m["score"] < 0 else "neutral"
        rationale.append(f"{m['name']}: {lean} — {m['detail']}")
    if mtf_score is not None:
        rationale.append(
            f"Multi-timeframe: {mtf['alignment']['verdict']} ({mtf['alignment']['detail']}, score {mtf_score:+.0f})"
        )
    if struct["nearest_resistance"]:
        rationale.append(
            f"Resistance {struct['nearest_resistance']['price']:.6g} ({struct['nearest_resistance']['distance_pct']:+.2f}%)"
        )
    if struct["nearest_support"]:
        rationale.append(
            f"Support {struct['nearest_support']['price']:.6g} ({struct['nearest_support']['distance_pct']:+.2f}%)"
        )
    rationale.append(f"Regime: {reg['name']} — {reg['detail']}")

    rr = None
    if struct["nearest_resistance"] and struct["nearest_support"]:
        upside = struct["nearest_resistance"]["price"] - price
        downside = price - struct["nearest_support"]["price"]
        if downside > 0:
            rr = round(upside / downside, 2)

    return {
        "ok": True,
        "symbol": symbol,
        "timeframe": timeframe,
        "ts": time.time(),
        "price": price,
        "horizon_bars": h,
        "horizon_label": _horizon_label(timeframe, h),
        "direction": direction,
        "direction_score": round(direction_score, 3),
        "probability_up": round(float(prob_up) * 100, 1),
        "probability_down": round((1 - float(prob_up)) * 100, 1),
        "expected_move_pct": round(move, 3),
        "target": target,
        "upper": price * (1 + (move + band) / 100.0),
        "lower": price * (1 + (move - band) / 100.0),
        "confidence": round(float(confidence) * 100, 1),
        "volatility": vol,
        "models": models,
        "levels": struct,
        "regime": reg,
        "risk_reward": rr,
        "path": path,
        "rationale": rationale,
        "bars": len(closes),
        "history": [float(c) for c in closes[-60:]],
        "elapsed_ms": round((time.time() - started) * 1000, 2),
    }


def _horizon_label(timeframe: str, bars: int) -> str:
    minutes = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440, "1w": 10080}.get(timeframe, 1) * bars
    if minutes < 60:
        return f"next {minutes} min"
    if minutes < 1440:
        return f"next {minutes / 60:.0f} h"
    if minutes < 10080:
        return f"next {minutes / 1440:.0f} d"
    return f"next {minutes / 10080:.0f} w"


def rank_forecasts(forecasts: list[dict[str, Any]], limit: int = 12) -> dict[str, list[dict[str, Any]]]:
    """Split predictions into the strongest up / down candidates."""
    valid = [f for f in forecasts if f.get("ok")]
    for f in valid:
        f["edge"] = round(abs(f["expected_move_pct"]) * (f["confidence"] / 100.0), 4)
    ups = sorted([f for f in valid if f["direction"] == "up"], key=lambda f: -f["edge"])[:limit]
    downs = sorted([f for f in valid if f["direction"] == "down"], key=lambda f: -f["edge"])[:limit]
    return {
        "up": [_slim(f) for f in ups],
        "down": [_slim(f) for f in downs],
        "all": [_slim(f) for f in sorted(valid, key=lambda f: -f["edge"])[: limit * 2]],
    }


def _headline(f: dict[str, Any]) -> str:
    """Pick the rationale line that best explains the predicted direction."""
    lines = f.get("rationale") or []
    want = "bullish" if f.get("direction") == "up" else "bearish" if f.get("direction") == "down" else None
    if want:
        for line in lines:
            if want in line:
                return line
    return lines[0] if lines else ""


def _slim(f: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": f.get("symbol"),
        "price": f.get("price"),
        "direction": f.get("direction"),
        "probability_up": f.get("probability_up"),
        "expected_move_pct": f.get("expected_move_pct"),
        "confidence": f.get("confidence"),
        "target": f.get("target"),
        "edge": f.get("edge"),
        "horizon_label": f.get("horizon_label"),
        "regime": (f.get("regime") or {}).get("name"),
        "risk_reward": f.get("risk_reward"),
        "top_reason": _headline(f),
    }
