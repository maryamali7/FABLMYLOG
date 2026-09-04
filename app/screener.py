from __future__ import annotations

import time
from typing import Any

import numpy as np

from app.indicators import (
    RollingWindow,
    adx,
    atr,
    bollinger,
    clamp,
    ema,
    hv,
    last_valid,
    roc,
    rsi,
    supertrend,
    zscore,
)


def _ret(closes: list[float], n: int) -> float:
    if len(closes) <= n or closes[-1 - n] == 0:
        return 0.0
    return (closes[-1] / closes[-1 - n] - 1.0) * 100


def features(symbol: str, win: RollingWindow, ticker, btc_ret: float) -> dict[str, Any] | None:
    if len(win) < 25:
        return None
    closes = list(win.closes)
    highs = list(win.highs)
    lows = list(win.lows)
    vols = list(win.volumes)
    px = ticker.last if ticker else closes[-1]
    chg = ticker.change_pct if ticker else _ret(closes, min(60, len(closes) - 1))
    r = last_valid(rsi(closes, 14)) or 50.0
    z = last_valid(zscore(closes, 20)) or 0.0
    roc12 = last_valid(roc(closes, 12)) or 0.0
    lo, mid, hi = bollinger(closes, 20, 2.0)
    width = 0.0
    if last_valid(mid) and last_valid(hi) and last_valid(lo) and mid[-1]:
        width = float((hi[-1] - lo[-1]) / (abs(mid[-1]) + 1e-9))
    avg_vol = float(np.mean(vols[-20:])) if vols else 0
    vol_ratio = (vols[-1] / avg_vol) if avg_vol else 1.0
    adx_line, pdi, mdi = adx(highs, lows, closes)
    adx_v = last_valid(adx_line) or 0.0
    pdi_v = last_valid(pdi) or 0.0
    mdi_v = last_valid(mdi) or 0.0
    a = last_valid(atr(highs, lows, closes)) or 0.0
    atr_pct = (a / px * 100) if px else 0.0
    hh = max(highs[-20:-1]) if len(highs) > 21 else max(highs)
    ll = min(lows[-20:-1]) if len(lows) > 21 else min(lows)
    breaking_up = px >= hh
    breaking_dn = px <= ll
    st, direction = supertrend(highs, lows, closes)
    st_dir = int(direction[-1]) if len(direction) else 0
    rs_btc = chg - btc_ret
    e9 = ema(closes, 9)[-1]
    e21 = ema(closes, 21)[-1]
    trend = "up" if e9 > e21 else "down"
    squeeze = width < 0.018
    vol_spike = vol_ratio >= 1.8
    # composite alpha 0-100
    alpha = 50.0
    alpha += clamp(chg / 8, -1, 1) * 12
    alpha += clamp(rs_btc / 6, -1, 1) * 14
    alpha += clamp((r - 50) / 50, -1, 1) * -8  # fade extremes slightly, reward mid-trend
    if trend == "up":
        alpha += 8
    else:
        alpha -= 6
    if vol_spike:
        alpha += 7
    if breaking_up:
        alpha += 10
    if breaking_dn:
        alpha -= 10
    if squeeze:
        alpha += 4
    alpha += clamp((adx_v - 20) / 25, -1, 1) * 8
    if st_dir == 1:
        alpha += 6
    else:
        alpha -= 4
    alpha = float(clamp(alpha, 0, 100) * 1)  # already 0-100-ish
    alpha = max(0.0, min(100.0, alpha))
    return {
        "symbol": symbol,
        "last": px,
        "change_pct": chg,
        "rsi": round(r, 1),
        "zscore": round(float(z), 2),
        "roc": round(float(roc12), 2),
        "bb_width": round(width * 100, 2),
        "vol_ratio": round(vol_ratio, 2),
        "adx": round(adx_v, 1),
        "plus_di": round(pdi_v, 1),
        "minus_di": round(mdi_v, 1),
        "atr_pct": round(atr_pct, 3),
        "hv": round(hv(closes) * 100, 1),
        "trend": trend,
        "supertrend": "bull" if st_dir == 1 else "bear",
        "squeeze": squeeze,
        "breakout": breaking_up,
        "breakdown": breaking_dn,
        "rs_btc": round(rs_btc, 2),
        "spread_bps": round(ticker.spread_bps, 2) if ticker else 0,
        "volume": ticker.volume if ticker else (vols[-1] if vols else 0),
        "alpha": round(alpha, 1),
        "bias": "long" if alpha >= 58 else ("short" if alpha <= 42 else "neutral"),
    }


def _top(rows: list[dict], key: str, n: int = 12, reverse: bool = True) -> list[dict]:
    valid = [r for r in rows if r.get(key) is not None]
    valid.sort(key=lambda r: r[key], reverse=reverse)
    return valid[:n]


def build_boards(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    if not rows:
        return {k: [] for k in BOARD_KEYS}
    return {
        "alpha": _top(rows, "alpha", 15),
        "gainers": _top(rows, "change_pct", 12),
        "losers": _top(rows, "change_pct", 12, reverse=False),
        "volume": _top(rows, "vol_ratio", 12),
        "rsi_os": [r for r in _top(rows, "rsi", 20, reverse=False) if r["rsi"] <= 38][:12],
        "rsi_ob": [r for r in _top(rows, "rsi", 20) if r["rsi"] >= 62][:12],
        "squeeze": [r for r in _top(rows, "bb_width", 20, reverse=False) if r["squeeze"]][:12],
        "breakout": [r for r in rows if r["breakout"]][:12] or _top(rows, "roc", 8),
        "trend": [r for r in _top(rows, "adx", 15) if r["adx"] >= 18],
        "rel_strength": _top(rows, "rs_btc", 12),
        "mean_rev": [r for r in rows if abs(r["zscore"]) >= 1.5],
        "volatility": _top(rows, "atr_pct", 12),
        "hot_money": [
            r
            for r in _top(rows, "alpha", 20)
            if r["vol_ratio"] >= 1.4 and r["change_pct"] > 0 and r["trend"] == "up"
        ][:12],
        "dump_bounce": [
            r for r in rows if r["change_pct"] <= -4 and r["rsi"] <= 35 and r["zscore"] <= -1.4
        ][:12],
    }


BOARD_KEYS = [
    "alpha",
    "gainers",
    "losers",
    "volume",
    "rsi_os",
    "rsi_ob",
    "squeeze",
    "breakout",
    "trend",
    "rel_strength",
    "mean_rev",
    "volatility",
    "hot_money",
    "dump_bounce",
]

BOARD_META = {
    "alpha": {"title": "Alpha score", "blurb": "Composite trend + flow + relative strength"},
    "gainers": {"title": "Momentum longs", "blurb": "Fastest 24h / tape gainers"},
    "losers": {"title": "Washout shorts", "blurb": "Hardest tape losers"},
    "volume": {"title": "Volume spike", "blurb": "Prints vs 20-bar average"},
    "rsi_os": {"title": "RSI oversold", "blurb": "Mean-reversion longs"},
    "rsi_ob": {"title": "RSI overbought", "blurb": "Exhausted rallies"},
    "squeeze": {"title": "Volatility squeeze", "blurb": "Tight Bollinger — break coming"},
    "breakout": {"title": "Range break", "blurb": "Donchian / 20-bar high break"},
    "trend": {"title": "ADX trend quality", "blurb": "Strong directional regimes"},
    "rel_strength": {"title": "vs BTC", "blurb": "Outperformers versus bitcoin"},
    "mean_rev": {"title": "Z-score extremes", "blurb": "Stretched vs 20-bar mean"},
    "volatility": {"title": "ATR expansion", "blurb": "Widest realized ranges"},
    "hot_money": {"title": "Hot money", "blurb": "Up-trend + volume + alpha"},
    "dump_bounce": {"title": "Dump bounce", "blurb": "Capitulation candidates"},
}


def scan(hub, symbols: list[str]) -> dict[str, Any]:
    btc = hub.quote("BTC/USDT")
    btc_win = hub.candles.get("BTC/USDT")
    btc_ret = 0.0
    if btc:
        btc_ret = btc.change_pct
    elif btc_win and len(btc_win) > 30:
        btc_ret = _ret(list(btc_win.closes), 30)
    rows: list[dict[str, Any]] = []
    heatmap: list[dict[str, Any]] = []
    for sym in symbols:
        win = hub.candles.get(sym)
        if not win or len(win) < 25:
            continue
        t = hub.quote(sym)
        feat = features(sym, win, t, btc_ret)
        if not feat:
            continue
        rows.append(feat)
        heatmap.append(
            {
                "symbol": sym,
                "change_pct": feat["change_pct"],
                "alpha": feat["alpha"],
                "rsi": feat["rsi"],
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
    }
