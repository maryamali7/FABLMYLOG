"""Advanced multi-factor crypto screener.

Adds on top of the original board scanner:

* ~40 factors per symbol (trend / momentum / volatility / flow / structure)
* composite **alpha**, **quality**, **risk** and **liquidity** scores with grades
* a **query engine** — arbitrary field filters, sorting, text search, pagination
* **saved presets** (curated screens) and CSV export
* extra boards: confluence, macd crosses, vwap reclaims, low-risk trends, shorts
"""

from __future__ import annotations

import csv
import io
import time
from typing import Any

import numpy as np

from app.indicators import RollingWindow, clamp
from app.rules import (
    ALL_FIELDS,
    FRAMES,
    context_at,
    context_from_row,
    evaluate_rule,
)


def _ret(closes: list[float], n: int) -> float:
    if len(closes) <= n or closes[-1 - n] == 0:
        return 0.0
    return (closes[-1] / closes[-1 - n] - 1.0) * 100


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    n = min(len(a), len(b))
    if n < 12:
        return 0.0
    x, y = a[-n:], b[-n:]
    if np.std(x) == 0 or np.std(y) == 0:
        return 0.0
    return float(np.clip(np.corrcoef(x, y)[0, 1], -1, 1))


def _grade(quality: float, risk: float) -> str:
    score = quality - risk * 0.35
    if score >= 62:
        return "A"
    if score >= 50:
        return "B"
    if score >= 38:
        return "C"
    return "D"


def features(
    symbol: str,
    win: RollingWindow,
    ticker,
    btc_ret: float,
    frame: dict[str, np.ndarray] | None = None,
    btc_returns: np.ndarray | None = None,
) -> dict[str, Any] | None:
    """Full factor row for one symbol."""
    if len(win) < 25:
        return None
    if frame is None:
        frame = FRAMES.get(symbol, win)
    if not frame:
        return None
    ctx = context_at(frame)
    g = lambda k, d=0.0: float(ctx.get(k, d))  # noqa: E731

    closes = list(win.closes)
    px = ticker.last if ticker and ticker.last else g("close", closes[-1])
    chg = ticker.change_pct if ticker else _ret(closes, min(60, len(closes) - 1))
    spread = round(ticker.spread_bps, 2) if ticker else 0.0
    volume = ticker.volume if ticker else g("volume")

    r = g("rsi", 50.0)
    adx_v = g("adx")
    atr_pct = g("atr_pct")
    vol_ratio = g("vol_ratio", 1.0)
    trend_score = g("trend_score", 50.0)
    mom_score = g("mom_score", 50.0)
    bb_width = g("bb_width")
    squeeze = bool(g("squeeze")) or bb_width < 1.8
    st_dir = int(g("st_dir", 1))
    breaking_up = px >= g("hh20", px) * 0.999
    breaking_dn = px <= g("ll20", px) * 1.001
    rs_btc = chg - btc_ret
    trend = "up" if g("ema9") > g("ema21") else "down"

    # ---- composite scores ------------------------------------------------
    alpha = 50.0
    alpha += clamp(chg / 8, -1, 1) * 12
    alpha += clamp(rs_btc / 6, -1, 1) * 14
    alpha += clamp((r - 50) / 50, -1, 1) * -8
    alpha += 8 if trend == "up" else -6
    alpha += 7 if vol_ratio >= 1.8 else 0
    alpha += 10 if breaking_up else 0
    alpha -= 10 if breaking_dn else 0
    alpha += 4 if squeeze else 0
    alpha += clamp((adx_v - 20) / 25, -1, 1) * 8
    alpha += 6 if st_dir == 1 else -4
    alpha = float(max(0.0, min(100.0, alpha)))

    risk_score = 0.0
    risk_score += clamp(atr_pct / 2.5, 0, 1) * 34
    risk_score += clamp(g("hv") / 180, 0, 1) * 22
    risk_score += clamp(spread / 40, 0, 1) * 22
    risk_score += clamp(abs(g("zscore")) / 3, 0, 1) * 12
    risk_score += 10 if adx_v < 15 else 0
    risk_score = float(max(0.0, min(100.0, risk_score)))

    liquidity = float(
        max(
            0.0,
            min(
                100.0,
                clamp(np.log10(max(volume, 1.0)) / 9, 0, 1) * 70 + (30 - clamp(spread / 30, 0, 1) * 30),
            ),
        )
    )

    # confluence: how many independent bullish reads agree
    checks = {
        "ema_stack": g("ema_stack") > 0,
        "supertrend": st_dir == 1,
        "macd": g("macd_hist") > 0,
        "adx": adx_v >= 20 and g("plus_di") > g("minus_di"),
        "volume": vol_ratio >= 1.3,
        "vwap": g("vwap_dev") > 0,
        "cloud": g("cloud_pos") > 0,
        "structure": g("dc_pos") >= 60,
        "momentum": mom_score >= 58,
        "obv": g("obv_slope") > 0,
    }
    signal_count = int(sum(1 for v in checks.values() if v))
    bear_count = int(sum(1 for v in checks.values() if not v))

    quality = float(
        max(
            0.0,
            min(
                100.0,
                trend_score * 0.28 + mom_score * 0.22 + signal_count * 2.8 + alpha * 0.16 + liquidity * 0.06,
            ),
        )
    )

    corr = 0.0
    if btc_returns is not None and len(frame.get("close", [])) > 15:
        c = frame["close"]
        rets = np.diff(c) / np.clip(c[:-1], 1e-12, None)
        corr = _corr(rets, btc_returns)

    row: dict[str, Any] = {
        "symbol": symbol,
        "last": px,
        "change_pct": chg,
        "rsi": round(r, 1),
        "rsi7": round(g("rsi7", 50), 1),
        "zscore": round(g("zscore"), 2),
        "roc": round(g("roc"), 2),
        "bb_width": round(bb_width, 2),
        "bb_pct": round(g("bb_pct", 0.5), 3),
        "vol_ratio": round(vol_ratio, 2),
        "vol_z": round(g("vol_z"), 2),
        "adx": round(adx_v, 1),
        "plus_di": round(g("plus_di"), 1),
        "minus_di": round(g("minus_di"), 1),
        "atr_pct": round(atr_pct, 3),
        "hv": round(g("hv"), 1),
        "macd_hist": round(g("macd_hist"), 6),
        "macd": round(g("macd"), 6),
        "macd_signal": round(g("macd_signal"), 6),
        "stoch_k": round(g("stoch_k"), 1),
        "stoch_d": round(g("stoch_d"), 1),
        "srsi_k": round(g("srsi_k"), 1),
        "cci": round(g("cci"), 1),
        "willr": round(g("willr"), 1),
        "obv_slope": round(g("obv_slope"), 4),
        "vwap_dev": round(g("vwap_dev"), 3),
        "buy_pressure": round(g("buy_pressure"), 1),
        "ema_stack": int(g("ema_stack")),
        "ema9_dev": round(g("ema9_dev"), 3),
        "ema50_dev": round(g("ema50_dev"), 3),
        "ema200_dev": round(g("ema200_dev"), 3),
        "dc_pos": round(g("dc_pos"), 1),
        "dist_hh_pct": round(g("dist_hh_pct"), 3),
        "dist_ll_pct": round(g("dist_ll_pct"), 3),
        "cloud_pos": int(g("cloud_pos")),
        "ret5": round(g("ret5"), 3),
        "ret15": round(g("ret15"), 3),
        "ret60": round(g("ret60"), 3),
        "range_pct": round(g("range_pct"), 3),
        "trend_score": round(trend_score, 1),
        "mom_score": round(mom_score, 1),
        "trend": trend,
        "supertrend": "bull" if st_dir == 1 else "bear",
        "st_dir": st_dir,
        "squeeze": squeeze,
        "breakout": bool(breaking_up),
        "breakdown": bool(breaking_dn),
        "rs_btc": round(rs_btc, 2),
        "corr_btc": round(corr, 2),
        "spread_bps": spread,
        "volume": volume,
        "liquidity": round(liquidity, 1),
        "risk_score": round(risk_score, 1),
        "quality": round(quality, 1),
        "signal_count": signal_count,
        "bear_count": bear_count,
        "alpha": round(alpha, 1),
    }
    row["bias"] = "long" if alpha >= 58 else ("short" if alpha <= 42 else "neutral")
    row["grade"] = _grade(quality, risk_score)
    row["confluence"] = [k for k, v in checks.items() if v]
    return row


# --------------------------------------------------------------------------- #
# boards
# --------------------------------------------------------------------------- #


def _top(rows: list[dict], key: str, n: int = 12, reverse: bool = True) -> list[dict]:
    valid = [r for r in rows if r.get(key) is not None]
    valid.sort(key=lambda r: r.get(key, 0), reverse=reverse)
    return valid[:n]


def build_boards(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    if not rows:
        return {k: [] for k in BOARD_KEYS}
    g = lambda r, k, d=0: r.get(k, d) or 0  # noqa: E731
    return {
        "alpha": _top(rows, "alpha", 15),
        "gainers": _top(rows, "change_pct", 12),
        "losers": _top(rows, "change_pct", 12, reverse=False),
        "volume": _top(rows, "vol_ratio", 12),
        "rsi_os": [r for r in _top(rows, "rsi", 20, reverse=False) if r.get("rsi", 50) <= 38][:12],
        "rsi_ob": [r for r in _top(rows, "rsi", 20) if r.get("rsi", 50) >= 62][:12],
        "squeeze": [r for r in _top(rows, "bb_width", 20, reverse=False) if r.get("squeeze")][:12],
        "breakout": [r for r in rows if r.get("breakout")][:12] or _top(rows, "roc", 8),
        "trend": [r for r in _top(rows, "adx", 15) if g(r, "adx") >= 18],
        "rel_strength": _top(rows, "rs_btc", 12),
        "mean_rev": [r for r in rows if abs(g(r, "zscore")) >= 1.5],
        "volatility": _top(rows, "atr_pct", 12),
        "hot_money": [
            r
            for r in _top(rows, "alpha", 20)
            if g(r, "vol_ratio") >= 1.4 and g(r, "change_pct") > 0 and r.get("trend") == "up"
        ][:12],
        "dump_bounce": [
            r
            for r in rows
            if g(r, "change_pct") <= -4 and g(r, "rsi", 50) <= 35 and g(r, "zscore") <= -1.4
        ][:12],
        # ---- new advanced boards
        "confluence": [r for r in _top(rows, "signal_count", 20) if g(r, "signal_count") >= 6][:12],
        "quality": [r for r in _top(rows, "quality", 20) if r.get("grade") in ("A", "B")][:12],
        "macd_cross": [
            r
            for r in rows
            if g(r, "macd_hist") > 0 and abs(g(r, "macd_hist")) < abs(g(r, "macd", 1)) * 0.35
        ][:12],
        "vwap_reclaim": [
            r for r in rows if 0 <= g(r, "vwap_dev") <= 0.6 and g(r, "trend_score") >= 55
        ][:12],
        "low_risk": [
            r
            for r in _top(rows, "quality", 30)
            if g(r, "risk_score") <= 45 and g(r, "liquidity") >= 55
        ][:12],
        "short_setups": [
            r
            for r in _top(rows, "bear_count", 20)
            if r.get("supertrend") == "bear" and g(r, "rsi", 50) >= 55 and g(r, "trend_score") < 45
        ][:12],
        "coiled": [
            r
            for r in rows
            if r.get("squeeze") and g(r, "adx") < 20 and abs(g(r, "bb_pct", 0.5) - 0.5) < 0.35
        ][:12],
        "liquid": _top(rows, "liquidity", 12),
        # ---- multi-timeframe + forecast boards
        "mtf_bull": [
            r
            for r in _top(rows, "mtf_score", 25)
            if g(r, "mtf_score") >= 35 and g(r, "mtf_agreement") >= 60 and g(r, "mtf_timeframes") >= 3
        ][:12],
        "mtf_bear": [
            r
            for r in _top(rows, "mtf_score", 25, reverse=False)
            if g(r, "mtf_score") <= -35 and g(r, "mtf_agreement") >= 60 and g(r, "mtf_timeframes") >= 3
        ][:12],
        "mtf_conflict": [
            r
            for r in rows
            if g(r, "mtf_timeframes") >= 3
            and g(r, "mtf_agreement") <= 45
            and (g(r, "mtf_overbought") >= 1 or g(r, "mtf_oversold") >= 1)
        ][:12],
        "tf_stacked_ob": [
            r for r in _top(rows, "mtf_overbought", 20) if g(r, "mtf_overbought") >= 2
        ][:12],
        "tf_stacked_os": [
            r for r in _top(rows, "mtf_oversold", 20) if g(r, "mtf_oversold") >= 2
        ][:12],
        "forecast_up": [
            r
            for r in _top(rows, "forecast_edge", 25)
            if r.get("forecast_dir") == "up" and g(r, "prob_up") >= 55
        ][:12],
        "forecast_down": [
            r
            for r in _top(rows, "forecast_edge", 25)
            if r.get("forecast_dir") == "down" and g(r, "prob_up") <= 45
        ][:12],
        "forecast_conviction": [
            r for r in _top(rows, "forecast_conf", 25) if g(r, "forecast_conf") >= 55
        ][:12],
    }


SCAN_BARS = 360

BOARD_KEYS = [
    "alpha",
    "confluence",
    "quality",
    "gainers",
    "losers",
    "volume",
    "rsi_os",
    "rsi_ob",
    "squeeze",
    "coiled",
    "breakout",
    "macd_cross",
    "vwap_reclaim",
    "trend",
    "rel_strength",
    "mean_rev",
    "volatility",
    "hot_money",
    "dump_bounce",
    "low_risk",
    "short_setups",
    "liquid",
    "mtf_bull",
    "mtf_bear",
    "mtf_conflict",
    "tf_stacked_ob",
    "tf_stacked_os",
    "forecast_up",
    "forecast_down",
    "forecast_conviction",
]

BOARD_META = {
    "alpha": {"title": "Alpha score", "blurb": "Composite trend + flow + relative strength"},
    "confluence": {"title": "Confluence", "blurb": "6+ independent bullish factors aligned"},
    "quality": {"title": "Setup quality", "blurb": "Graded A/B setups — trend, momentum, liquidity"},
    "gainers": {"title": "Momentum longs", "blurb": "Fastest 24h / tape gainers"},
    "losers": {"title": "Washout shorts", "blurb": "Hardest tape losers"},
    "volume": {"title": "Volume spike", "blurb": "Prints vs 20-bar average"},
    "rsi_os": {"title": "RSI oversold", "blurb": "Mean-reversion longs"},
    "rsi_ob": {"title": "RSI overbought", "blurb": "Exhausted rallies"},
    "squeeze": {"title": "Volatility squeeze", "blurb": "Tight Bollinger — break coming"},
    "coiled": {"title": "Coiled range", "blurb": "Squeeze with no trend yet — pre-break"},
    "breakout": {"title": "Range break", "blurb": "Donchian / 20-bar high break"},
    "macd_cross": {"title": "Fresh MACD cross", "blurb": "Histogram just flipped positive"},
    "vwap_reclaim": {"title": "VWAP reclaim", "blurb": "Just back above VWAP with trend support"},
    "trend": {"title": "ADX trend quality", "blurb": "Strong directional regimes"},
    "rel_strength": {"title": "vs BTC", "blurb": "Outperformers versus bitcoin"},
    "mean_rev": {"title": "Z-score extremes", "blurb": "Stretched vs 20-bar mean"},
    "volatility": {"title": "ATR expansion", "blurb": "Widest realized ranges"},
    "hot_money": {"title": "Hot money", "blurb": "Up-trend + volume + alpha"},
    "dump_bounce": {"title": "Dump bounce", "blurb": "Capitulation candidates"},
    "low_risk": {"title": "Low risk trend", "blurb": "Quality setups with tame volatility"},
    "short_setups": {"title": "Short setups", "blurb": "Bearish structure with room to fall"},
    "liquid": {"title": "Deepest books", "blurb": "Best liquidity / tightest spreads"},
    "mtf_bull": {"title": "MTF aligned long", "blurb": "Timeframes stacked bullish 1m → 1w"},
    "mtf_bear": {"title": "MTF aligned short", "blurb": "Timeframes stacked bearish 1m → 1w"},
    "mtf_conflict": {"title": "Timeframe conflict", "blurb": "Frames disagree — chop / turn risk"},
    "tf_stacked_ob": {"title": "Overbought stack", "blurb": "RSI overbought on 2+ timeframes"},
    "tf_stacked_os": {"title": "Oversold stack", "blurb": "RSI oversold on 2+ timeframes"},
    "forecast_up": {"title": "Predicted up", "blurb": "Ensemble expects a move higher"},
    "forecast_down": {"title": "Predicted down", "blurb": "Ensemble expects a move lower"},
    "forecast_conviction": {"title": "High conviction", "blurb": "Strongest model agreement"},
}


# --------------------------------------------------------------------------- #
# query engine
# --------------------------------------------------------------------------- #

SORTABLE = [k for k in ALL_FIELDS] + [
    "alpha",
    "quality",
    "risk_score",
    "liquidity",
    "signal_count",
    "change_pct",
    "volume",
    "symbol",
]

PRESETS: list[dict[str, Any]] = [
    {
        "id": "mtf_long_stack",
        "title": "Multi-TF long stack",
        "blurb": "Higher timeframes trending up with 65%+ agreement",
        "sort": "mtf_score",
        "filters": [
            {"left": "mtf_score", "cmp": ">", "right": 40},
            {"left": "mtf_agreement", "cmp": ">", "right": 65},
            {"left": "mtf_timeframes", "cmp": ">", "right": 2},
        ],
    },
    {
        "id": "mtf_pullback_buy",
        "title": "HTF trend, LTF dip",
        "blurb": "1h trending up while the 15m is oversold — pullback entries",
        "sort": "mtf_score",
        "filters": [
            {"left": "trend_1h", "cmp": "==", "right": "up"},
            {"left": "rsi_15m", "cmp": "<", "right": 40},
            {"left": "adx_1h", "cmp": ">", "right": 18},
        ],
    },
    {
        "id": "exhausted_stack",
        "title": "Overbought on every frame",
        "blurb": "RSI stretched on 2+ timeframes — fade / take profit",
        "sort": "mtf_overbought",
        "filters": [
            {"left": "mtf_overbought", "cmp": ">", "right": 1},
            {"left": "change_pct", "cmp": ">", "right": 2},
        ],
    },
    {
        "id": "predicted_movers",
        "title": "Predicted movers",
        "blurb": "Ensemble forecast: 58%+ probability with real expected range",
        "sort": "forecast_edge",
        "filters": [
            {"left": "prob_up", "cmp": ">", "right": 58},
            {"left": "forecast_conf", "cmp": ">", "right": 50},
            {"left": "forecast_rr", "cmp": ">", "right": 1.2},
        ],
    },
    {
        "id": "breakout_ready",
        "title": "Breakout ready",
        "blurb": "Coiled + volume + inside 1.5% of the 20-bar high",
        "sort": "quality",
        "filters": [
            {"left": "dist_hh_pct", "cmp": "<", "right": 1.5},
            {"left": "vol_ratio", "cmp": ">", "right": 1.3},
            {"left": "adx", "cmp": ">", "right": 15},
        ],
    },
    {
        "id": "oversold_reversal",
        "title": "Oversold reversal",
        "blurb": "RSI < 32, z-score flush, buyers stepping in",
        "sort": "alpha",
        "filters": [
            {"left": "rsi", "cmp": "<", "right": 32},
            {"left": "zscore", "cmp": "<", "right": -1.3},
            {"left": "buy_pressure", "cmp": ">", "right": 45},
        ],
    },
    {
        "id": "trend_quality",
        "title": "Clean trend",
        "blurb": "EMA stack, ADX > 22, price over VWAP",
        "sort": "trend_score",
        "filters": [
            {"left": "ema_stack", "cmp": ">", "right": 0},
            {"left": "adx", "cmp": ">", "right": 22},
            {"left": "vwap_dev", "cmp": ">", "right": 0},
        ],
    },
    {
        "id": "high_confluence",
        "title": "High confluence",
        "blurb": "7+ bullish factors and a grade of A or B",
        "sort": "signal_count",
        "filters": [
            {"left": "signal_count", "cmp": ">=", "right": 7},
            {"left": "grade", "cmp": "!=", "right": "D"},
        ],
    },
    {
        "id": "low_risk_alpha",
        "title": "Low-risk alpha",
        "blurb": "Alpha over 60 without paying for volatility",
        "sort": "alpha",
        "filters": [
            {"left": "alpha", "cmp": ">", "right": 60},
            {"left": "risk_score", "cmp": "<", "right": 45},
            {"left": "liquidity", "cmp": ">", "right": 55},
        ],
    },
    {
        "id": "volatility_hunters",
        "title": "Volatility hunters",
        "blurb": "Wide ATR and expanding ranges for scalps",
        "sort": "atr_pct",
        "filters": [
            {"left": "atr_pct", "cmp": ">", "right": 0.6},
            {"left": "vol_ratio", "cmp": ">", "right": 1.2},
        ],
    },
    {
        "id": "btc_outperformers",
        "title": "BTC outperformers",
        "blurb": "Beating bitcoin with low correlation",
        "sort": "rs_btc",
        "filters": [
            {"left": "rs_btc", "cmp": ">", "right": 1.0},
            {"left": "corr_btc", "cmp": "<", "right": 0.7},
        ],
    },
    {
        "id": "short_pressure",
        "title": "Short pressure",
        "blurb": "Bearish supertrend, weak structure, still elevated RSI",
        "sort": "risk_score",
        "filters": [
            {"left": "supertrend", "cmp": "==", "right": "bear"},
            {"left": "trend_score", "cmp": "<", "right": 42},
            {"left": "rsi", "cmp": ">", "right": 50},
        ],
    },
]


def preset(preset_id: str) -> dict[str, Any] | None:
    for p in PRESETS:
        if p["id"] == preset_id:
            return p
    return None


def run_query(
    rows: list[dict[str, Any]],
    filters: Any = None,
    sort_by: str = "alpha",
    sort_dir: str = "desc",
    limit: int = 60,
    search: str = "",
    preset_id: str | None = None,
    match: str = "all",
) -> dict[str, Any]:
    """Filter / sort / paginate screener rows using the shared rule engine."""
    started = time.time()
    if preset_id:
        p = preset(preset_id)
        if p:
            filters = p["filters"] if not filters else filters
            sort_by = p.get("sort", sort_by)
    rule: Any = None
    if filters:
        if isinstance(filters, dict) and (filters.get("rules") is not None or "op" in filters):
            rule = filters
        else:
            rule = {"op": "all" if match not in ("any", "none") else match, "rules": list(filters)}

    q = (search or "").strip().upper()
    out: list[dict[str, Any]] = []
    errors: list[str] = []
    for row in rows:
        if q and q not in str(row.get("symbol", "")).upper():
            continue
        if rule is not None:
            try:
                ok, _trace = evaluate_rule(rule, context_from_row(row))
            except Exception as exc:  # pragma: no cover - defensive
                errors.append(str(exc))
                ok = False
            if not ok:
                continue
        out.append(row)

    reverse = str(sort_dir).lower() != "asc"
    key = sort_by if sort_by else "alpha"

    def sort_key(r: dict[str, Any]):
        v = r.get(key)
        if v is None:
            return (1, 0.0)
        if isinstance(v, str):
            return (0, v)
        return (0, float(v))

    try:
        out.sort(key=sort_key, reverse=reverse)
    except TypeError:
        out.sort(key=lambda r: str(r.get(key, "")), reverse=reverse)

    total = len(out)
    limit = max(1, min(int(limit or 60), 500))
    return {
        "rows": out[:limit],
        "total": total,
        "returned": min(total, limit),
        "sort_by": key,
        "sort_dir": "desc" if reverse else "asc",
        "errors": errors[:3],
        "elapsed_ms": round((time.time() - started) * 1000, 2),
    }


MTF_COLUMNS = ["mtf_score", "mtf_agreement", "rsi_1h", "trend_1h", "prob_up", "exp_move", "forecast_conf"]

DEFAULT_COLUMNS = [
    "symbol",
    "last",
    "change_pct",
    "alpha",
    "quality",
    "grade",
    "rsi",
    "adx",
    "trend_score",
    "mom_score",
    "vol_ratio",
    "atr_pct",
    "risk_score",
    "liquidity",
    "rs_btc",
    "signal_count",
    "bias",
]


def rows_to_csv(rows: list[dict[str, Any]], columns: list[str] | None = None) -> str:
    cols = columns or DEFAULT_COLUMNS
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({c: r.get(c, "") for c in cols})
    return buf.getvalue()


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Market-wide breadth stats shown above the screener table."""
    if not rows:
        return {"n": 0}
    ups = [r for r in rows if (r.get("change_pct") or 0) > 0]
    alphas = [r.get("alpha", 50) for r in rows]
    return {
        "n": len(rows),
        "advancers": len(ups),
        "decliners": len(rows) - len(ups),
        "breadth_pct": round(len(ups) / len(rows) * 100, 1),
        "avg_alpha": round(float(np.mean(alphas)), 1),
        "avg_change": round(float(np.mean([r.get("change_pct", 0) or 0 for r in rows])), 2),
        "avg_risk": round(float(np.mean([r.get("risk_score", 0) or 0 for r in rows])), 1),
        "squeezes": sum(1 for r in rows if r.get("squeeze")),
        "breakouts": sum(1 for r in rows if r.get("breakout")),
        "grade_a": sum(1 for r in rows if r.get("grade") == "A"),
        "risk_on": round(
            float(np.mean([1 if r.get("trend") == "up" else 0 for r in rows]) * 100), 1
        ),
    }


# --------------------------------------------------------------------------- #
# scan
# --------------------------------------------------------------------------- #


def scan(hub, symbols: list[str]) -> dict[str, Any]:
    btc = hub.quote("BTC/USDT")
    btc_win = hub.candles.get("BTC/USDT")
    btc_ret = 0.0
    if btc:
        btc_ret = btc.change_pct
    elif btc_win and len(btc_win) > 30:
        btc_ret = _ret(list(btc_win.closes), 30)
    btc_returns = None
    if btc_win and len(btc_win) > 20:
        btc_win = btc_win.tail(SCAN_BARS)
        bc = np.asarray(list(btc_win.closes), dtype=float)
        btc_returns = np.diff(bc) / np.clip(bc[:-1], 1e-12, None)

    rows: list[dict[str, Any]] = []
    heatmap: list[dict[str, Any]] = []
    for sym in symbols:
        win = hub.candles.get(sym)
        if win:
            win = win.tail(SCAN_BARS)
        if not win or len(win) < 25:
            continue
        t = hub.quote(sym)
        try:
            feat = features(sym, win, t, btc_ret, btc_returns=btc_returns)
        except Exception:
            feat = None
        if not feat:
            continue
        rows.append(feat)
        heatmap.append(
            {
                "symbol": sym,
                "change_pct": feat["change_pct"],
                "alpha": feat["alpha"],
                "rsi": feat["rsi"],
                "quality": feat["quality"],
                "grade": feat["grade"],
            }
        )
    boards = build_boards(rows)
    alerts = []
    for r in boards.get("alpha", [])[:5]:
        if r["alpha"] >= 72:
            alerts.append(
                {
                    "ts": time.time(),
                    "kind": "alpha",
                    "symbol": r["symbol"],
                    "text": f"{r['symbol']} alpha {r['alpha']} · {r['bias']} · RSI {r['rsi']}",
                }
            )
    for r in boards.get("confluence", [])[:3]:
        alerts.append(
            {
                "ts": time.time(),
                "kind": "confluence",
                "symbol": r["symbol"],
                "text": f"{r['symbol']} {r['signal_count']}/10 factors aligned · grade {r['grade']}",
            }
        )
    for r in boards.get("hot_money", [])[:3]:
        alerts.append(
            {
                "ts": time.time(),
                "kind": "flow",
                "symbol": r["symbol"],
                "text": f"hot money {r['symbol']} vol {r['vol_ratio']}x  {r['change_pct']:+.2f}%",
            }
        )
    for r in boards.get("dump_bounce", [])[:3]:
        alerts.append(
            {
                "ts": time.time(),
                "kind": "bounce",
                "symbol": r["symbol"],
                "text": f"capitulation {r['symbol']} RSI {r['rsi']} z {r['zscore']}",
            }
        )
    heatmap.sort(key=lambda x: x["change_pct"], reverse=True)
    return {
        "ts": time.time(),
        "rows": rows,
        "boards": boards,
        "meta": BOARD_META,
        "alerts": alerts[:12],
        "heatmap": heatmap,
        "btc_ret": btc_ret,
        "n": len(rows),
        "summary": summarize(rows),
        "presets": [{k: p[k] for k in ("id", "title", "blurb")} for p in PRESETS],
        "columns": DEFAULT_COLUMNS,
    }
