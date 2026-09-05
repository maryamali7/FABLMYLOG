"""Feature frames + a safe rule engine.

This module is the backbone for:

* the **custom strategy builder** (visual rules -> live signals)
* the **advanced screener** (field filters, presets, ranking)
* the **backtester** (same rules replayed bar by bar)
* **custom alert rules**

A ``Frame`` is a dict of aligned numpy arrays computed once per candle series
(cached per symbol/bar) so that rules can be evaluated at any bar index without
recomputing indicators.
"""

from __future__ import annotations

import ast
import math
import operator
import time
from typing import Any, Callable, Iterable

import numpy as np

from app.indicators import (
    RollingWindow,
    adx,
    atr,
    ema,
    macd,
    obv,
    rsi,
    sma,
    supertrend,
)

# --------------------------------------------------------------------------- #
# fast rolling helpers
# --------------------------------------------------------------------------- #


def _win_view(arr: np.ndarray, period: int) -> np.ndarray:
    period = max(1, int(period))
    if len(arr) < period:
        pad = np.full(period - len(arr), arr[0] if len(arr) else 0.0)
        arr = np.concatenate([pad, arr])
    return np.lib.stride_tricks.sliding_window_view(arr, period)


def _roll(arr: np.ndarray, period: int, fn: Callable[[np.ndarray], np.ndarray]) -> np.ndarray:
    a = np.asarray(arr, dtype=float)
    n = len(a)
    if n == 0:
        return a
    view = _win_view(a, period)
    vals = fn(view)
    out = np.full(n, np.nan)
    take = min(n, len(vals))
    out[n - take :] = vals[len(vals) - take :]
    # front-fill the warm-up region so rules never explode on NaN
    first = out[~np.isnan(out)]
    if len(first):
        out = np.where(np.isnan(out), first[0], out)
    else:
        out = np.zeros(n)
    return out


def roll_max(arr, period: int) -> np.ndarray:
    return _roll(arr, period, lambda v: v.max(axis=-1))


def roll_min(arr, period: int) -> np.ndarray:
    return _roll(arr, period, lambda v: v.min(axis=-1))


def roll_mean(arr, period: int) -> np.ndarray:
    return _roll(arr, period, lambda v: v.mean(axis=-1))


def roll_std(arr, period: int) -> np.ndarray:
    return _roll(arr, period, lambda v: v.std(axis=-1))


def roll_sum(arr, period: int) -> np.ndarray:
    return _roll(arr, period, lambda v: v.sum(axis=-1))


def _safe_div(a, b, default: float = 0.0) -> np.ndarray:
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    out = np.full(np.broadcast(a, b).shape, float(default))
    np.divide(a, b, out=out, where=np.abs(b) > 1e-12)
    return np.nan_to_num(out, nan=default, posinf=default, neginf=default)


def _pct_change(arr: np.ndarray, n: int) -> np.ndarray:
    a = np.asarray(arr, dtype=float)
    out = np.zeros(len(a))
    if len(a) > n:
        prev = a[:-n]
        out[n:] = _safe_div(a[n:] - prev, np.abs(prev)) * 100.0
    return out


def _clean(arr, default: float = 0.0) -> np.ndarray:
    return np.nan_to_num(np.asarray(arr, dtype=float), nan=default, posinf=default, neginf=default)


# --------------------------------------------------------------------------- #
# field catalog (drives the builder / screener UI)
# --------------------------------------------------------------------------- #

FIELDS: dict[str, dict[str, Any]] = {
    # price / structure
    "close": {"label": "Close", "group": "Price", "fmt": "price"},
    "open": {"label": "Open", "group": "Price", "fmt": "price"},
    "high": {"label": "High", "group": "Price", "fmt": "price"},
    "low": {"label": "Low", "group": "Price", "fmt": "price"},
    "hlc3": {"label": "Typical price", "group": "Price", "fmt": "price"},
    "ret1": {"label": "Bar return %", "group": "Price", "fmt": "pct"},
    "ret5": {"label": "5-bar return %", "group": "Price", "fmt": "pct"},
    "ret15": {"label": "15-bar return %", "group": "Price", "fmt": "pct"},
    "ret60": {"label": "60-bar return %", "group": "Price", "fmt": "pct"},
    "body_pct": {"label": "Candle body %", "group": "Price", "fmt": "pct"},
    "range_pct": {"label": "Candle range %", "group": "Price", "fmt": "pct"},
    "upper_wick_pct": {"label": "Upper wick %", "group": "Price", "fmt": "pct"},
    "lower_wick_pct": {"label": "Lower wick %", "group": "Price", "fmt": "pct"},
    # moving averages
    "ema9": {"label": "EMA 9", "group": "Trend", "fmt": "price"},
    "ema21": {"label": "EMA 21", "group": "Trend", "fmt": "price"},
    "ema50": {"label": "EMA 50", "group": "Trend", "fmt": "price"},
    "ema200": {"label": "EMA 200", "group": "Trend", "fmt": "price"},
    "sma20": {"label": "SMA 20", "group": "Trend", "fmt": "price"},
    "sma50": {"label": "SMA 50", "group": "Trend", "fmt": "price"},
    "ema_stack": {"label": "EMA stack (-1/0/1)", "group": "Trend", "fmt": "num"},
    "ema9_dev": {"label": "Price vs EMA9 %", "group": "Trend", "fmt": "pct"},
    "ema50_dev": {"label": "Price vs EMA50 %", "group": "Trend", "fmt": "pct"},
    "ema200_dev": {"label": "Price vs EMA200 %", "group": "Trend", "fmt": "pct"},
    "trend_score": {"label": "Trend score 0-100", "group": "Trend", "fmt": "num"},
    # oscillators
    "rsi": {"label": "RSI 14", "group": "Momentum", "fmt": "num"},
    "rsi7": {"label": "RSI 7", "group": "Momentum", "fmt": "num"},
    "stoch_k": {"label": "Stochastic %K", "group": "Momentum", "fmt": "num"},
    "stoch_d": {"label": "Stochastic %D", "group": "Momentum", "fmt": "num"},
    "srsi_k": {"label": "Stoch RSI %K", "group": "Momentum", "fmt": "num"},
    "srsi_d": {"label": "Stoch RSI %D", "group": "Momentum", "fmt": "num"},
    "macd": {"label": "MACD line", "group": "Momentum", "fmt": "num"},
    "macd_signal": {"label": "MACD signal", "group": "Momentum", "fmt": "num"},
    "macd_hist": {"label": "MACD histogram", "group": "Momentum", "fmt": "num"},
    "cci": {"label": "CCI 20", "group": "Momentum", "fmt": "num"},
    "willr": {"label": "Williams %R", "group": "Momentum", "fmt": "num"},
    "roc": {"label": "Rate of change", "group": "Momentum", "fmt": "pct"},
    "mom_score": {"label": "Momentum score 0-100", "group": "Momentum", "fmt": "num"},
    # volatility / channels
    "atr": {"label": "ATR", "group": "Volatility", "fmt": "price"},
    "atr_pct": {"label": "ATR %", "group": "Volatility", "fmt": "pct"},
    "bb_upper": {"label": "Bollinger upper", "group": "Volatility", "fmt": "price"},
    "bb_mid": {"label": "Bollinger mid", "group": "Volatility", "fmt": "price"},
    "bb_lower": {"label": "Bollinger lower", "group": "Volatility", "fmt": "price"},
    "bb_width": {"label": "Bollinger width %", "group": "Volatility", "fmt": "pct"},
    "bb_pct": {"label": "%B position", "group": "Volatility", "fmt": "num"},
    "kc_upper": {"label": "Keltner upper", "group": "Volatility", "fmt": "price"},
    "kc_lower": {"label": "Keltner lower", "group": "Volatility", "fmt": "price"},
    "squeeze": {"label": "In squeeze (0/1)", "group": "Volatility", "fmt": "bool"},
    "hv": {"label": "Realized vol %", "group": "Volatility", "fmt": "pct"},
    "zscore": {"label": "Z-score 20", "group": "Volatility", "fmt": "num"},
    # channels / regime
    "dc_high": {"label": "Donchian high", "group": "Structure", "fmt": "price"},
    "dc_low": {"label": "Donchian low", "group": "Structure", "fmt": "price"},
    "dc_pos": {"label": "Donchian position %", "group": "Structure", "fmt": "num"},
    "hh20": {"label": "20-bar high", "group": "Structure", "fmt": "price"},
    "ll20": {"label": "20-bar low", "group": "Structure", "fmt": "price"},
    "dist_hh_pct": {"label": "Distance to 20-bar high %", "group": "Structure", "fmt": "pct"},
    "dist_ll_pct": {"label": "Distance from 20-bar low %", "group": "Structure", "fmt": "pct"},
    "adx": {"label": "ADX", "group": "Structure", "fmt": "num"},
    "plus_di": {"label": "+DI", "group": "Structure", "fmt": "num"},
    "minus_di": {"label": "-DI", "group": "Structure", "fmt": "num"},
    "st_dir": {"label": "Supertrend dir", "group": "Structure", "fmt": "num"},
    "tenkan": {"label": "Ichimoku tenkan", "group": "Structure", "fmt": "price"},
    "kijun": {"label": "Ichimoku kijun", "group": "Structure", "fmt": "price"},
    "cloud_pos": {"label": "Cloud position", "group": "Structure", "fmt": "num"},
    # flow
    "volume": {"label": "Volume", "group": "Flow", "fmt": "num"},
    "vol_sma20": {"label": "Volume SMA20", "group": "Flow", "fmt": "num"},
    "vol_ratio": {"label": "Volume vs average", "group": "Flow", "fmt": "num"},
    "vol_z": {"label": "Volume z-score", "group": "Flow", "fmt": "num"},
    "obv": {"label": "OBV", "group": "Flow", "fmt": "num"},
    "obv_slope": {"label": "OBV slope", "group": "Flow", "fmt": "num"},
    "vwap": {"label": "VWAP", "group": "Flow", "fmt": "price"},
    "vwap_dev": {"label": "Price vs VWAP %", "group": "Flow", "fmt": "pct"},
    "buy_pressure": {"label": "Buy pressure 0-100", "group": "Flow", "fmt": "num"},
}

# extra fields only present on screener rows (not in the candle frame)
SCREENER_ONLY_FIELDS: dict[str, dict[str, Any]] = {
    "alpha": {"label": "Alpha score", "group": "Screener", "fmt": "num"},
    "change_pct": {"label": "24h change %", "group": "Screener", "fmt": "pct"},
    "rs_btc": {"label": "Relative strength vs BTC", "group": "Screener", "fmt": "num"},
    "spread_bps": {"label": "Spread bps", "group": "Screener", "fmt": "num"},
    "quality": {"label": "Setup quality 0-100", "group": "Screener", "fmt": "num"},
    "risk_score": {"label": "Risk score 0-100", "group": "Screener", "fmt": "num"},
    "liquidity": {"label": "Liquidity score 0-100", "group": "Screener", "fmt": "num"},
    "corr_btc": {"label": "Correlation to BTC", "group": "Screener", "fmt": "num"},
    "signal_count": {"label": "Confluence signals", "group": "Screener", "fmt": "num"},
    "bias": {"label": "Bias (long/short/neutral)", "group": "Screener", "fmt": "text"},
    "trend": {"label": "Trend (up/down)", "group": "Screener", "fmt": "text"},
    "supertrend": {"label": "Supertrend (bull/bear)", "group": "Screener", "fmt": "text"},
    "grade": {"label": "Grade A-D", "group": "Screener", "fmt": "text"},
    "last": {"label": "Last price", "group": "Screener", "fmt": "price"},
    "breakout": {"label": "Breaking out (0/1)", "group": "Screener", "fmt": "bool"},
    "breakdown": {"label": "Breaking down (0/1)", "group": "Screener", "fmt": "bool"},
    "bear_count": {"label": "Bearish factors", "group": "Screener", "fmt": "num"},
    "symbol": {"label": "Symbol", "group": "Screener", "fmt": "text"},
}

# multi-timeframe + forecast fields (merged onto screener rows by the robot)
MTF_TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d", "1w"]
MTF_FIELDS: dict[str, dict[str, Any]] = {}
for _tf in MTF_TIMEFRAMES:
    MTF_FIELDS[f"rsi_{_tf}"] = {"label": f"RSI {_tf}", "group": "Timeframes", "fmt": "num"}
    MTF_FIELDS[f"score_{_tf}"] = {"label": f"Rating {_tf} (-100..100)", "group": "Timeframes", "fmt": "num"}
    MTF_FIELDS[f"trend_{_tf}"] = {"label": f"Trend {_tf} (up/down/flat)", "group": "Timeframes", "fmt": "text"}
    MTF_FIELDS[f"adx_{_tf}"] = {"label": f"ADX {_tf}", "group": "Timeframes", "fmt": "num"}
MTF_FIELDS.update(
    {
        "mtf_score": {"label": "MTF alignment score", "group": "Timeframes", "fmt": "num"},
        "mtf_agreement": {"label": "MTF agreement %", "group": "Timeframes", "fmt": "num"},
        "mtf_bias": {"label": "MTF bias (long/short)", "group": "Timeframes", "fmt": "text"},
        "mtf_verdict": {"label": "MTF verdict", "group": "Timeframes", "fmt": "text"},
        "mtf_timeframes": {"label": "Timeframes loaded", "group": "Timeframes", "fmt": "num"},
        "mtf_overbought": {"label": "Timeframes overbought", "group": "Timeframes", "fmt": "num"},
        "mtf_oversold": {"label": "Timeframes oversold", "group": "Timeframes", "fmt": "num"},
    }
)

FORECAST_FIELDS: dict[str, dict[str, Any]] = {
    "prob_up": {"label": "Probability up %", "group": "Forecast", "fmt": "num"},
    "exp_move": {"label": "Expected move %", "group": "Forecast", "fmt": "pct"},
    "forecast_conf": {"label": "Forecast confidence %", "group": "Forecast", "fmt": "num"},
    "forecast_dir": {"label": "Forecast direction", "group": "Forecast", "fmt": "text"},
    "forecast_edge": {"label": "Forecast edge", "group": "Forecast", "fmt": "num"},
    "forecast_rr": {"label": "Forecast risk/reward", "group": "Forecast", "fmt": "num"},
    "regime": {"label": "Regime", "group": "Forecast", "fmt": "text"},
    "support_dist": {"label": "Distance to support %", "group": "Forecast", "fmt": "pct"},
    "resistance_dist": {"label": "Distance to resistance %", "group": "Forecast", "fmt": "pct"},
}

ALL_FIELDS = {**FIELDS, **SCREENER_ONLY_FIELDS, **MTF_FIELDS, **FORECAST_FIELDS}

COMPARATORS: dict[str, dict[str, Any]] = {
    ">": {"label": "greater than", "arity": 2},
    ">=": {"label": "at least", "arity": 2},
    "<": {"label": "less than", "arity": 2},
    "<=": {"label": "at most", "arity": 2},
    "==": {"label": "equals", "arity": 2},
    "!=": {"label": "not equal", "arity": 2},
    "between": {"label": "between", "arity": 3},
    "outside": {"label": "outside", "arity": 3},
    "cross_above": {"label": "crosses above", "arity": 2},
    "cross_below": {"label": "crosses below", "arity": 2},
    "rising": {"label": "is rising", "arity": 1},
    "falling": {"label": "is falling", "arity": 1},
    "is_true": {"label": "is true", "arity": 1},
    "is_false": {"label": "is false", "arity": 1},
    "contains": {"label": "contains", "arity": 2},
}

GROUP_OPS = ("all", "any", "none")


# --------------------------------------------------------------------------- #
# Frame
# --------------------------------------------------------------------------- #


class Feat(float):
    """A float that remembers its previous bar value (enables cross rules)."""

    __slots__ = ("prev",)

    def __new__(cls, value: float, prev: float | None = None):
        try:
            v = float(value)
        except (TypeError, ValueError):
            v = 0.0
        if math.isnan(v) or math.isinf(v):
            v = 0.0
        obj = super().__new__(cls, v)
        try:
            p = float(prev) if prev is not None else v
        except (TypeError, ValueError):
            p = v
        obj.prev = 0.0 if (math.isnan(p) or math.isinf(p)) else p
        return obj


def _as_arrays(source: Any, max_bars: int) -> dict[str, np.ndarray]:
    if isinstance(source, RollingWindow):
        o = list(source.opens)
        h = list(source.highs)
        low = list(source.lows)
        c = list(source.closes)
        v = list(source.volumes)
        ts = list(source.ts)
    elif isinstance(source, dict):
        o, h, low, c, v, ts = (
            source.get("open", []),
            source.get("high", []),
            source.get("low", []),
            source.get("close", []),
            source.get("volume", []),
            source.get("ts", []),
        )
    else:  # iterable of candle-ish objects / dicts
        rows = list(source)
        get = lambda r, k: r.get(k) if isinstance(r, dict) else getattr(r, k)  # noqa: E731
        o = [float(get(r, "open")) for r in rows]
        h = [float(get(r, "high")) for r in rows]
        low = [float(get(r, "low")) for r in rows]
        c = [float(get(r, "close")) for r in rows]
        v = [float(get(r, "volume")) for r in rows]
        ts = [float(get(r, "ts")) for r in rows]
    n = len(c)
    if max_bars and n > max_bars:
        s = n - max_bars
        o, h, low, c, v, ts = o[s:], h[s:], low[s:], c[s:], v[s:], ts[s:]
    return {
        "open": np.asarray(o, dtype=float),
        "high": np.asarray(h, dtype=float),
        "low": np.asarray(low, dtype=float),
        "close": np.asarray(c, dtype=float),
        "volume": np.asarray(v, dtype=float),
        "ts": np.asarray(ts, dtype=float),
    }


MIN_BARS = 30


def compute_frame(source: Any, max_bars: int = 320) -> dict[str, np.ndarray]:
    """Compute every builder field as an aligned numpy array."""
    base = _as_arrays(source, max_bars)
    c = base["close"]
    n = len(c)
    if n == 0:
        return {}
    o, h, low, v = base["open"], base["high"], base["low"], base["volume"]
    f: dict[str, np.ndarray] = dict(base)
    f["hlc3"] = (h + low + c) / 3.0

    f["ret1"] = _pct_change(c, 1)
    f["ret5"] = _pct_change(c, 5)
    f["ret15"] = _pct_change(c, 15)
    f["ret60"] = _pct_change(c, 60)

    rng = np.maximum(h - low, 1e-12)
    f["range_pct"] = _safe_div(h - low, c) * 100
    f["body_pct"] = _safe_div(np.abs(c - o), c) * 100
    f["upper_wick_pct"] = _safe_div(h - np.maximum(o, c), rng) * 100
    f["lower_wick_pct"] = _safe_div(np.minimum(o, c) - low, rng) * 100

    for p in (9, 21, 50, 200):
        f[f"ema{p}"] = _clean(ema(c, p))
    f["sma20"] = _clean(sma(c, 20), default=float(c[0]))
    f["sma50"] = _clean(sma(c, 50), default=float(c[0]))
    f["ema9_dev"] = _safe_div(c - f["ema9"], f["ema9"]) * 100
    f["ema50_dev"] = _safe_div(c - f["ema50"], f["ema50"]) * 100
    f["ema200_dev"] = _safe_div(c - f["ema200"], f["ema200"]) * 100
    stack = np.zeros(n)
    stack[(f["ema9"] > f["ema21"]) & (f["ema21"] > f["ema50"])] = 1
    stack[(f["ema9"] < f["ema21"]) & (f["ema21"] < f["ema50"])] = -1
    f["ema_stack"] = stack

    f["rsi"] = _clean(rsi(c, 14), default=50.0)
    f["rsi7"] = _clean(rsi(c, 7), default=50.0)

    hh14 = roll_max(h, 14)
    ll14 = roll_min(low, 14)
    f["stoch_k"] = _safe_div(c - ll14, hh14 - ll14, 0.5) * 100
    f["stoch_d"] = roll_mean(f["stoch_k"], 3)
    r = f["rsi"]
    rhi = roll_max(r, 14)
    rlo = roll_min(r, 14)
    f["srsi_k"] = _safe_div(r - rlo, rhi - rlo, 0.5) * 100
    f["srsi_d"] = roll_mean(f["srsi_k"], 3)

    m_line, m_sig, m_hist = macd(c, 12, 26, 9)
    f["macd"] = _clean(m_line)
    f["macd_signal"] = _clean(m_sig)
    f["macd_hist"] = _clean(m_hist)

    tp = f["hlc3"]
    tp_sma = roll_mean(tp, 20)
    md = _roll(np.abs(tp - tp_sma), 20, lambda w: w.mean(axis=-1))
    f["cci"] = _safe_div(tp - tp_sma, 0.015 * md)
    hh = roll_max(h, 14)
    ll = roll_min(low, 14)
    f["willr"] = _safe_div(hh - c, hh - ll, 0.5) * -100
    f["roc"] = _pct_change(c, 12)

    a = _clean(atr(h, low, c, 14))
    f["atr"] = a
    f["atr_pct"] = _safe_div(a, c) * 100

    mid = roll_mean(c, 20)
    sd = roll_std(c, 20)
    f["bb_mid"] = mid
    f["bb_upper"] = mid + 2 * sd
    f["bb_lower"] = mid - 2 * sd
    f["bb_width"] = _safe_div(f["bb_upper"] - f["bb_lower"], mid) * 100
    f["bb_pct"] = _safe_div(c - f["bb_lower"], f["bb_upper"] - f["bb_lower"], 0.5)
    kc_mid = f["ema21"]
    f["kc_upper"] = kc_mid + 1.5 * a
    f["kc_lower"] = kc_mid - 1.5 * a
    f["squeeze"] = (
        (f["bb_upper"] < f["kc_upper"]) & (f["bb_lower"] > f["kc_lower"])
    ).astype(float)

    logret = np.zeros(n)
    safe_c = np.clip(c, 1e-12, None)
    logret[1:] = np.diff(np.log(safe_c))
    f["hv"] = roll_std(logret, 20) * math.sqrt(365 * 24 * 60) * 100
    f["zscore"] = _safe_div(c - mid, sd)

    dc_hi = roll_max(h, 20)
    dc_lo = roll_min(low, 20)
    f["dc_high"] = dc_hi
    f["dc_low"] = dc_lo
    f["dc_pos"] = _safe_div(c - dc_lo, dc_hi - dc_lo, 0.5) * 100
    f["hh20"] = dc_hi
    f["ll20"] = dc_lo
    f["dist_hh_pct"] = _safe_div(dc_hi - c, c) * 100
    f["dist_ll_pct"] = _safe_div(c - dc_lo, c) * 100

    adx_line, pdi, mdi = adx(h, low, c, 14)
    f["adx"] = _clean(adx_line)
    f["plus_di"] = _clean(pdi)
    f["minus_di"] = _clean(mdi)
    _st, st_dir = supertrend(h, low, c, 10, 3.0)
    f["st_dir"] = _clean(st_dir, default=1.0)

    tenkan = (roll_max(h, 9) + roll_min(low, 9)) / 2.0
    kijun = (roll_max(h, 26) + roll_min(low, 26)) / 2.0
    span_a = (tenkan + kijun) / 2.0
    span_b = (roll_max(h, 52) + roll_min(low, 52)) / 2.0
    f["tenkan"] = tenkan
    f["kijun"] = kijun
    cloud_top = np.maximum(span_a, span_b)
    cloud_bot = np.minimum(span_a, span_b)
    pos = np.zeros(n)
    pos[c > cloud_top] = 1
    pos[c < cloud_bot] = -1
    f["cloud_pos"] = pos

    vol_sma = roll_mean(v, 20)
    f["vol_sma20"] = vol_sma
    f["vol_ratio"] = _safe_div(v, vol_sma, 1.0)
    f["vol_z"] = _safe_div(v - vol_sma, roll_std(v, 20))
    ob = _clean(obv(c, v))
    f["obv"] = ob
    f["obv_slope"] = _safe_div(ob - np.roll(ob, 10), np.abs(roll_mean(np.abs(ob), 20)) + 1e-9)
    f["obv_slope"][:10] = 0.0
    cum_pv = np.cumsum(tp * v)
    cum_v = np.cumsum(v)
    f["vwap"] = _safe_div(cum_pv, cum_v, default=float(c[0]))
    f["vwap"] = np.where(cum_v > 0, f["vwap"], c)
    f["vwap_dev"] = _safe_div(c - f["vwap"], f["vwap"]) * 100
    f["buy_pressure"] = _safe_div(c - low, rng, 0.5) * 100

    # composite scores
    trend = np.full(n, 50.0)
    trend += np.clip(f["ema9_dev"] / 3.0, -1, 1) * 12
    trend += np.clip((f["adx"] - 20) / 25.0, -1, 1) * 14
    trend += f["ema_stack"] * 10
    trend += f["st_dir"] * 8
    trend += f["cloud_pos"] * 6
    f["trend_score"] = np.clip(trend, 0, 100)

    mom = np.full(n, 50.0)
    mom += np.clip(f["roc"] / 6.0, -1, 1) * 16
    mom += np.clip((f["rsi"] - 50) / 30.0, -1, 1) * 12
    mom += np.clip(f["macd_hist"] / (np.abs(c) * 0.004 + 1e-9), -1, 1) * 10
    mom += np.clip((f["vol_ratio"] - 1) / 1.5, -1, 1) * 8
    f["mom_score"] = np.clip(mom, 0, 100)

    for k, arr in list(f.items()):
        f[k] = _clean(arr)
    f["_n"] = np.asarray([n], dtype=float)
    return f


# --------------------------------------------------------------------------- #
# frame cache (recomputes at most once per new bar per symbol)
# --------------------------------------------------------------------------- #


class FrameCache:
    def __init__(self, ttl: float = 20.0):
        self._data: dict[str, tuple[Any, float, dict[str, np.ndarray]]] = {}
        self.ttl = ttl
        self.hits = 0
        self.misses = 0

    def get(self, symbol: str, win: RollingWindow, max_bars: int = 320) -> dict[str, np.ndarray]:
        n = len(win)
        last_bucket = math.floor((win.ts[-1] if n else 0) / 60)
        key = (n, last_bucket)
        hit = self._data.get(symbol)
        now = time.time()
        if hit and hit[0] == key and now - hit[1] < self.ttl:
            self.hits += 1
            return hit[2]
        self.misses += 1
        frame = compute_frame(win, max_bars)
        self._data[symbol] = (key, now, frame)
        return frame

    def clear(self) -> None:
        self._data.clear()


FRAMES = FrameCache()


def context_at(frame: dict[str, np.ndarray], i: int = -1, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a rule-evaluation context for a single bar."""
    if not frame:
        return dict(extra or {})
    n = len(frame["close"])
    if n == 0:
        return dict(extra or {})
    idx = i if i >= 0 else n + i
    idx = max(0, min(n - 1, idx))
    prev = max(0, idx - 1)
    ctx: dict[str, Any] = {}
    for k, arr in frame.items():
        if k.startswith("_") or k == "ts":
            continue
        try:
            ctx[k] = Feat(arr[idx], arr[prev])
        except (IndexError, TypeError):
            continue
    ctx["price"] = ctx.get("close", Feat(0))
    ctx["bar_index"] = Feat(idx)
    if extra:
        for k, val in extra.items():
            ctx[k] = Feat(val, val) if isinstance(val, (int, float)) and not isinstance(val, bool) else val
    return ctx


def context_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """Context from a flat screener row (numbers stay numbers, strings stay strings)."""
    ctx: dict[str, Any] = {}
    for k, val in row.items():
        if isinstance(val, bool):
            ctx[k] = 1.0 if val else 0.0
        elif isinstance(val, (int, float)):
            ctx[k] = Feat(val, val)
        else:
            ctx[k] = val
    return ctx


# --------------------------------------------------------------------------- #
# safe expression evaluation
# --------------------------------------------------------------------------- #


def _prev_of(x: Any) -> float:
    return float(getattr(x, "prev", x) or 0.0)


def fn_cross_above(a: Any, b: Any) -> bool:
    return float(a) > float(b) and _prev_of(a) <= _prev_of(b)


def fn_cross_below(a: Any, b: Any) -> bool:
    return float(a) < float(b) and _prev_of(a) >= _prev_of(b)


def fn_rising(a: Any) -> bool:
    return float(a) > _prev_of(a)


def fn_falling(a: Any) -> bool:
    return float(a) < _prev_of(a)


def fn_pct_diff(a: Any, b: Any) -> float:
    b = float(b)
    if abs(b) < 1e-12:
        return 0.0
    return (float(a) - b) / abs(b) * 100.0


SAFE_FUNCS: dict[str, Callable[..., Any]] = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "sqrt": lambda x: math.sqrt(max(0.0, float(x))),
    "cross_above": fn_cross_above,
    "cross_below": fn_cross_below,
    "rising": fn_rising,
    "falling": fn_falling,
    "pct_diff": fn_pct_diff,
    "prev": _prev_of,
}

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.FloorDiv: operator.floordiv,
}
_CMP_OPS = {
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
}


class ExpressionError(ValueError):
    pass


def _eval_node(node: ast.AST, ctx: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, ctx)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, str, bool)) or node.value is None:
            return node.value
        raise ExpressionError("unsupported constant")
    if isinstance(node, ast.Name):
        if node.id in ctx:
            return ctx[node.id]
        if node.id in ("True", "true"):
            return True
        if node.id in ("False", "false"):
            return False
        raise ExpressionError(f"unknown field '{node.id}'")
    if isinstance(node, ast.BoolOp):
        vals = [_eval_node(v, ctx) for v in node.values]
        if isinstance(node.op, ast.And):
            return all(bool(v) for v in vals)
        return any(bool(v) for v in vals)
    if isinstance(node, ast.UnaryOp):
        val = _eval_node(node.operand, ctx)
        if isinstance(node.op, ast.Not):
            return not bool(val)
        if isinstance(node.op, ast.USub):
            return -float(val)
        if isinstance(node.op, ast.UAdd):
            return +float(val)
        raise ExpressionError("unsupported unary operator")
    if isinstance(node, ast.BinOp):
        fn = _BIN_OPS.get(type(node.op))
        if not fn:
            raise ExpressionError("unsupported operator")
        left = float(_eval_node(node.left, ctx))
        right = float(_eval_node(node.right, ctx))
        if fn in (operator.truediv, operator.mod, operator.floordiv) and abs(right) < 1e-12:
            return 0.0
        if fn is operator.pow and abs(right) > 8:
            raise ExpressionError("exponent too large")
        return fn(left, right)
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, ctx)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval_node(comp, ctx)
            fn = _CMP_OPS.get(type(op))
            if not fn:
                raise ExpressionError("unsupported comparison")
            if isinstance(left, str) or isinstance(right, str):
                ok = fn(str(left), str(right)) if type(op) in (ast.Eq, ast.NotEq) else False
            else:
                ok = fn(float(left), float(right))
            if not ok:
                return False
            left = right
        return True
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ExpressionError("unsupported call")
        fn = SAFE_FUNCS.get(node.func.id)
        if not fn:
            raise ExpressionError(f"unknown function '{node.func.id}'")
        if node.keywords:
            raise ExpressionError("keyword arguments are not supported")
        return fn(*[_eval_node(a, ctx) for a in node.args])
    if isinstance(node, ast.IfExp):
        return _eval_node(node.body, ctx) if bool(_eval_node(node.test, ctx)) else _eval_node(node.orelse, ctx)
    raise ExpressionError(f"unsupported syntax: {type(node).__name__}")


def eval_expression(expr: str, ctx: dict[str, Any]) -> Any:
    if not expr or not expr.strip():
        raise ExpressionError("empty expression")
    if len(expr) > 600:
        raise ExpressionError("expression too long")
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as exc:  # pragma: no cover - message varies
        raise ExpressionError(f"syntax error: {exc.msg}") from exc
    return _eval_node(tree, ctx)


# --------------------------------------------------------------------------- #
# rule evaluation
# --------------------------------------------------------------------------- #


def _operand(value: Any, ctx: dict[str, Any]) -> Any:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (list, tuple)):
        return [_operand(v, ctx) for v in value]
    text = str(value).strip()
    if text in ctx:
        return ctx[text]
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return eval_expression(text, ctx)
    except ExpressionError:
        return text  # plain string comparison (bias == "long")


def _fmt(v: Any) -> str:
    if isinstance(v, (int, float)):
        return f"{float(v):.4g}"
    return str(v)


def evaluate_condition(cond: dict[str, Any], ctx: dict[str, Any]) -> tuple[bool, str]:
    if "expr" in cond:
        expr = str(cond.get("expr") or "")
        try:
            ok = bool(eval_expression(expr, ctx))
        except ExpressionError as exc:
            return False, f"{expr} → error: {exc}"
        return ok, f"{expr} → {ok}"
    field = cond.get("left", cond.get("field"))
    op = str(cond.get("cmp") or cond.get("op") or ">")
    right = cond.get("right", cond.get("value"))
    left = _operand(field, ctx)
    label = str(field)
    if op in ("rising", "falling", "is_true", "is_false"):
        if op == "rising":
            ok = fn_rising(left)
        elif op == "falling":
            ok = fn_falling(left)
        elif op == "is_true":
            ok = bool(float(left)) if not isinstance(left, str) else bool(left)
        else:
            ok = not (bool(float(left)) if not isinstance(left, str) else bool(left))
        return ok, f"{label}({_fmt(left)}) {op} → {ok}"
    if op in ("between", "outside"):
        bounds = right if isinstance(right, (list, tuple)) else [cond.get("low"), cond.get("high")]
        lo = float(_operand(bounds[0] if len(bounds) > 0 else 0, ctx) or 0)
        hi = float(_operand(bounds[1] if len(bounds) > 1 else 0, ctx) or 0)
        if lo > hi:
            lo, hi = hi, lo
        inside = lo <= float(left) <= hi
        ok = inside if op == "between" else not inside
        return ok, f"{label}({_fmt(left)}) {op} {lo:g}..{hi:g} → {ok}"
    rv = _operand(right, ctx)
    if op == "cross_above":
        ok = fn_cross_above(left, rv)
    elif op == "cross_below":
        ok = fn_cross_below(left, rv)
    elif op == "contains":
        ok = str(rv).lower() in str(left).lower()
    elif isinstance(left, str) or isinstance(rv, str):
        ls, rs = str(left).lower(), str(rv).lower()
        ok = (ls == rs) if op in ("==", ">=", "<=") else (ls != rs) if op == "!=" else False
    else:
        lf, rf = float(left), float(rv)
        ok = {
            ">": lf > rf,
            ">=": lf >= rf,
            "<": lf < rf,
            "<=": lf <= rf,
            "==": abs(lf - rf) < 1e-9,
            "!=": abs(lf - rf) >= 1e-9,
        }.get(op, False)
    return ok, f"{label}({_fmt(left)}) {op} {_fmt(rv)} → {ok}"


def evaluate_rule(rule: Any, ctx: dict[str, Any]) -> tuple[bool, list[str]]:
    """Evaluate a rule node. Returns (passed, human readable trace)."""
    if rule is None:
        return True, []
    if isinstance(rule, str):
        ok, trace = evaluate_condition({"expr": rule}, ctx)
        return ok, [trace]
    if isinstance(rule, list):
        rule = {"op": "all", "rules": rule}
    if not isinstance(rule, dict):
        return False, ["invalid rule"]
    op = str(rule.get("op") or "").lower()
    if op in GROUP_OPS or "rules" in rule:
        op = op if op in GROUP_OPS else "all"
        children = rule.get("rules") or []
        if not children:
            return True, []
        results: list[bool] = []
        traces: list[str] = []
        for child in children:
            ok, tr = evaluate_rule(child, ctx)
            results.append(ok)
            traces.extend(tr)
        if op == "all":
            return all(results), traces
        if op == "any":
            return any(results), traces
        return not any(results), traces
    ok, trace = evaluate_condition(rule, ctx)
    return ok, [trace]


def count_conditions(rule: Any) -> int:
    if rule is None:
        return 0
    if isinstance(rule, str):
        return 1
    if isinstance(rule, list):
        return sum(count_conditions(r) for r in rule)
    if isinstance(rule, dict):
        if rule.get("rules") is not None:
            return sum(count_conditions(r) for r in rule.get("rules") or [])
        return 1
    return 0


def validate_rule(rule: Any, known: Iterable[str] | None = None) -> list[str]:
    """Static validation used by the builder before saving a strategy."""
    known_fields = set(known or ALL_FIELDS.keys()) | {"price", "bar_index"}
    errors: list[str] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 6:
            errors.append("rules nested too deeply (max 6 levels)")
            return
        if node is None:
            return
        if isinstance(node, str):
            _check_expr(node)
            return
        if isinstance(node, list):
            for child in node:
                walk(child, depth + 1)
            return
        if not isinstance(node, dict):
            errors.append(f"invalid rule node: {node!r}")
            return
        if node.get("rules") is not None:
            op = str(node.get("op") or "all").lower()
            if op not in GROUP_OPS:
                errors.append(f"unknown group operator '{op}'")
            for child in node.get("rules") or []:
                walk(child, depth + 1)
            return
        if "expr" in node:
            _check_expr(str(node.get("expr") or ""))
            return
        field = node.get("left", node.get("field"))
        cmp_op = str(node.get("cmp") or node.get("op") or "")
        if not field:
            errors.append("condition is missing a field")
        elif isinstance(field, str) and field not in known_fields:
            _check_expr(field)
        if cmp_op not in COMPARATORS:
            errors.append(f"unknown comparator '{cmp_op}'")
        arity = COMPARATORS.get(cmp_op, {}).get("arity", 2)
        right = node.get("right", node.get("value"))
        if arity >= 2 and right is None:
            errors.append(f"'{field} {cmp_op}' needs a value")
        if cmp_op in ("between", "outside"):
            bounds = right if isinstance(right, (list, tuple)) else []
            if len(bounds) != 2:
                errors.append(f"'{field} {cmp_op}' needs two bounds")
        if isinstance(right, str) and right and right not in known_fields:
            _check_expr(right, soft=True)

    def _check_expr(expr: str, soft: bool = False) -> None:
        probe = {k: Feat(1.0, 1.0) for k in known_fields}
        try:
            eval_expression(expr, probe)
        except ExpressionError as exc:
            # right-hand operands may legitimately be string literals ("bull", "A")
            if soft and "unknown field" in str(exc):
                return
            errors.append(f"{expr!r}: {exc}")

    walk(rule)
    return errors


def field_catalog() -> list[dict[str, Any]]:
    """Grouped field metadata for the builder / screener UI."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for key, meta in ALL_FIELDS.items():
        groups.setdefault(meta.get("group", "Other"), []).append(
            {"key": key, "label": meta.get("label", key), "fmt": meta.get("fmt", "num")}
        )
    return [{"group": g, "fields": sorted(v, key=lambda x: x["label"])} for g, v in groups.items()]
