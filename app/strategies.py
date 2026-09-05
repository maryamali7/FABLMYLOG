from __future__ import annotations

import time
from typing import Any

import numpy as np

from app.indicators import (
    RollingWindow,
    adx,
    atr,
    bollinger,
    cci,
    clamp,
    donchian,
    ema,
    ichimoku,
    keltner,
    last_valid,
    macd,
    roc,
    rsi,
    slope,
    stoch_rsi,
    supertrend,
    vwap,
    zscore,
)
from app.models import Signal, SignalKind


class Strategy:
    name = "base"
    title = "Base"
    family = "core"

    def __init__(self, params: dict[str, Any]):
        self.params = params
        self.enabled = bool(params.get("enabled", True))
        self.weight = float(params.get("weight", 1.0))

    def evaluate(self, symbol: str, win: RollingWindow, price: float) -> Signal | None:
        raise NotImplementedError

    def _sig(
        self,
        symbol: str,
        kind: SignalKind,
        confidence: float,
        price: float,
        reason: str,
        **extras,
    ) -> Signal:
        return Signal(
            strategy=self.name,
            symbol=symbol,
            kind=kind,
            confidence=clamp(confidence * self.weight if kind != SignalKind.HOLD else confidence),
            price=price,
            reason=reason,
            ts=time.time(),
            extras=extras,
        )


class RsiReversion(Strategy):
    name = "rsi_reversion"
    title = "RSI reversion"
    family = "mean-reversion"

    def evaluate(self, symbol: str, win: RollingWindow, price: float) -> Signal | None:
        period = int(self.params.get("period", 14))
        if len(win) < period + 5:
            return None
        series = rsi(list(win.closes), period)
        val = series[-1]
        if np.isnan(val):
            return None
        oversold = float(self.params.get("oversold", 32))
        overbought = float(self.params.get("overbought", 68))
        if val <= oversold:
            conf = (oversold - val) / max(oversold, 1) + 0.55
            return self._sig(symbol, SignalKind.BUY, conf, price, f"RSI {val:.1f} oversold", rsi=val)
        if val >= overbought:
            conf = (val - overbought) / max(100 - overbought, 1) + 0.55
            return self._sig(symbol, SignalKind.SELL, conf, price, f"RSI {val:.1f} overbought", rsi=val)
        return self._sig(symbol, SignalKind.HOLD, 0.2, price, f"RSI {val:.1f} neutral", rsi=val)


class MacdTrend(Strategy):
    name = "macd_trend"
    title = "MACD trend"
    family = "trend"

    def evaluate(self, symbol: str, win: RollingWindow, price: float) -> Signal | None:
        if len(win) < 40:
            return None
        line, sig, hist = macd(
            list(win.closes),
            int(self.params.get("fast", 12)),
            int(self.params.get("slow", 26)),
            int(self.params.get("signal", 9)),
        )
        if np.isnan(hist[-1]) or np.isnan(hist[-2]):
            return None
        crossed_up = hist[-2] <= 0 <= hist[-1]
        crossed_dn = hist[-2] >= 0 >= hist[-1]
        mag = abs(hist[-1]) / (price + 1e-9) * 8000
        if crossed_up:
            return self._sig(symbol, SignalKind.BUY, 0.55 + clamp(mag), price, "MACD histogram flipped +")
        if crossed_dn:
            return self._sig(symbol, SignalKind.SELL, 0.55 + clamp(mag), price, "MACD histogram flipped -")
        if hist[-1] > 0 and line[-1] > sig[-1]:
            return self._sig(symbol, SignalKind.BUY, 0.42, price, "MACD bullish regime")
        if hist[-1] < 0 and line[-1] < sig[-1]:
            return self._sig(symbol, SignalKind.SELL, 0.42, price, "MACD bearish regime")
        return None


class EmaTrend(Strategy):
    name = "ema_trend"
    title = "EMA ribbon"
    family = "trend"

    def evaluate(self, symbol: str, win: RollingWindow, price: float) -> Signal | None:
        fast_n = int(self.params.get("fast", 9))
        slow_n = int(self.params.get("slow", 21))
        if len(win) < slow_n + 5:
            return None
        closes = list(win.closes)
        f = ema(closes, fast_n)
        s = ema(closes, slow_n)
        gap = (f[-1] - s[-1]) / price
        sl = slope(f, 6)
        if f[-2] <= s[-2] and f[-1] > s[-1]:
            return self._sig(symbol, SignalKind.BUY, 0.62 + clamp(abs(gap) * 40), price, "EMA bullish cross")
        if f[-2] >= s[-2] and f[-1] < s[-1]:
            return self._sig(symbol, SignalKind.SELL, 0.62 + clamp(abs(gap) * 40), price, "EMA bearish cross")
        if f[-1] > s[-1] and sl > 0:
            return self._sig(symbol, SignalKind.BUY, 0.4 + clamp(sl * 80), price, "EMA uptrend")
        if f[-1] < s[-1] and sl < 0:
            return self._sig(symbol, SignalKind.SELL, 0.4 + clamp(abs(sl) * 80), price, "EMA downtrend")
        return None


class BollingerReversion(Strategy):
    name = "bollinger"
    title = "Bollinger bounce"
    family = "mean-reversion"

    def evaluate(self, symbol: str, win: RollingWindow, price: float) -> Signal | None:
        period = int(self.params.get("period", 20))
        if len(win) < period + 2:
            return None
        lo, mid, hi = bollinger(list(win.closes), period, float(self.params.get("std", 2.0)))
        if np.isnan(lo[-1]) or np.isnan(hi[-1]):
            return None
        width = (hi[-1] - lo[-1]) / (mid[-1] + 1e-9)
        if price <= lo[-1]:
            return self._sig(symbol, SignalKind.BUY, 0.58 + clamp((lo[-1] - price) / price * 20), price, "touch lower band")
        if price >= hi[-1]:
            return self._sig(symbol, SignalKind.SELL, 0.58 + clamp((price - hi[-1]) / price * 20), price, "touch upper band")
        if width < 0.01:
            return self._sig(symbol, SignalKind.HOLD, 0.15, price, "squeeze — wait")
        return None


class Breakout(Strategy):
    name = "breakout"
    title = "Volume breakout"
    family = "breakout"

    def evaluate(self, symbol: str, win: RollingWindow, price: float) -> Signal | None:
        n = int(self.params.get("lookback", 30))
        if len(win) < n + 2:
            return None
        highs = list(win.highs)[-n - 1 : -1]
        lows = list(win.lows)[-n - 1 : -1]
        vols = list(win.volumes)
        avg_vol = float(np.mean(vols[-n:])) if vols else 0
        last_vol = vols[-1] if vols else 0
        vol_ok = last_vol > avg_vol * 1.4 if avg_vol else True
        hh = max(highs)
        ll = min(lows)
        if price > hh and vol_ok:
            return self._sig(symbol, SignalKind.BUY, 0.66, price, f"volume breakout above {hh:.6g}")
        if price < ll and vol_ok:
            return self._sig(symbol, SignalKind.SELL, 0.66, price, f"volume breakdown below {ll:.6g}")
        return None


class GridHint(Strategy):
    name = "grid"
    title = "Grid zones"
    family = "market-make"

    def evaluate(self, symbol: str, win: RollingWindow, price: float) -> Signal | None:
        if len(win) < 40:
            return None
        closes = np.asarray(win.closes, dtype=float)
        rng = float(self.params.get("range_pct", 0.04))
        mid = float(np.median(closes[-40:]))
        if price < mid * (1 - rng / 2):
            return self._sig(symbol, SignalKind.BUY, 0.5, price, "grid lower zone")
        if price > mid * (1 + rng / 2):
            return self._sig(symbol, SignalKind.SELL, 0.5, price, "grid upper zone")
        return None


class SupertrendFollow(Strategy):
    name = "supertrend"
    title = "Supertrend"
    family = "trend"

    def evaluate(self, symbol: str, win: RollingWindow, price: float) -> Signal | None:
        if len(win) < 30:
            return None
        st, direction = supertrend(
            list(win.highs),
            list(win.lows),
            list(win.closes),
            int(self.params.get("period", 10)),
            float(self.params.get("mult", 3.0)),
        )
        if direction[-1] == 1 and direction[-2] == -1:
            return self._sig(symbol, SignalKind.BUY, 0.7, price, "Supertrend flip bull")
        if direction[-1] == -1 and direction[-2] == 1:
            return self._sig(symbol, SignalKind.SELL, 0.7, price, "Supertrend flip bear")
        if direction[-1] == 1:
            return self._sig(symbol, SignalKind.BUY, 0.44, price, "Supertrend long regime")
        return self._sig(symbol, SignalKind.SELL, 0.44, price, "Supertrend short regime")


class IchimokuKumo(Strategy):
    name = "ichimoku"
    title = "Ichimoku kumo"
    family = "trend"

    def evaluate(self, symbol: str, win: RollingWindow, price: float) -> Signal | None:
        if len(win) < 55:
            return None
        t, k, sa, sb = ichimoku(list(win.highs), list(win.lows), list(win.closes))
        tenkan, kijun, a, b = t[-1], k[-1], sa[-1], sb[-1]
        if any(np.isnan(x) for x in (tenkan, kijun, a, b)):
            return None
        cloud_top = max(a, b)
        cloud_bot = min(a, b)
        if price > cloud_top and tenkan > kijun:
            return self._sig(symbol, SignalKind.BUY, 0.68, price, "price above kumo, TK bull")
        if price < cloud_bot and tenkan < kijun:
            return self._sig(symbol, SignalKind.SELL, 0.68, price, "price below kumo, TK bear")
        return None


class StochRsiCross(Strategy):
    name = "stoch_rsi"
    title = "Stoch RSI"
    family = "momentum"

    def evaluate(self, symbol: str, win: RollingWindow, price: float) -> Signal | None:
        if len(win) < 40:
            return None
        k_line, d_line = stoch_rsi(list(win.closes))
        k, d = k_line[-1], d_line[-1]
        if np.isnan(k) or np.isnan(d) or np.isnan(k_line[-2]):
            return None
        if k_line[-2] <= d_line[-2] and k > d and k < 25:
            return self._sig(symbol, SignalKind.BUY, 0.64, price, f"StochRSI cross up {k:.0f}")
        if k_line[-2] >= d_line[-2] and k < d and k > 75:
            return self._sig(symbol, SignalKind.SELL, 0.64, price, f"StochRSI cross down {k:.0f}")
        return None


class VwapReversion(Strategy):
    name = "vwap"
    title = "VWAP magnet"
    family = "mean-reversion"

    def evaluate(self, symbol: str, win: RollingWindow, price: float) -> Signal | None:
        if len(win) < 30:
            return None
        series = vwap(list(win.highs), list(win.lows), list(win.closes), list(win.volumes))
        vw = last_valid(series)
        if not vw:
            return None
        dist = (price - vw) / vw
        if dist <= -0.006:
            return self._sig(symbol, SignalKind.BUY, 0.58 + clamp(abs(dist) * 20), price, f"{dist:.2%} below VWAP")
        if dist >= 0.006:
            return self._sig(symbol, SignalKind.SELL, 0.58 + clamp(dist * 20), price, f"{dist:.2%} above VWAP")
        return None


class AdxTrend(Strategy):
    name = "adx_trend"
    title = "ADX DI"
    family = "trend"

    def evaluate(self, symbol: str, win: RollingWindow, price: float) -> Signal | None:
        if len(win) < 40:
            return None
        adx_line, pdi, mdi = adx(list(win.highs), list(win.lows), list(win.closes))
        a, p, m = adx_line[-1], pdi[-1], mdi[-1]
        if np.isnan(a):
            return None
        if a < float(self.params.get("min_adx", 18)):
            return self._sig(symbol, SignalKind.HOLD, 0.12, price, f"ADX {a:.0f} chop")
        if p > m and a > 22:
            return self._sig(symbol, SignalKind.BUY, 0.5 + clamp((a - 22) / 40), price, f"ADX {a:.0f} +DI lead")
        if m > p and a > 22:
            return self._sig(symbol, SignalKind.SELL, 0.5 + clamp((a - 22) / 40), price, f"ADX {a:.0f} -DI lead")
        return None


class KeltnerBreak(Strategy):
    name = "keltner"
    title = "Keltner expansion"
    family = "breakout"

    def evaluate(self, symbol: str, win: RollingWindow, price: float) -> Signal | None:
        if len(win) < 30:
            return None
        lo, mid, hi = keltner(list(win.highs), list(win.lows), list(win.closes))
        if np.isnan(hi[-1]) or np.isnan(lo[-1]):
            return None
        if price > hi[-1]:
            return self._sig(symbol, SignalKind.BUY, 0.63, price, "Keltner upper break")
        if price < lo[-1]:
            return self._sig(symbol, SignalKind.SELL, 0.63, price, "Keltner lower break")
        return None


class DonchianTurtle(Strategy):
    name = "donchian"
    title = "Donchian turtle"
    family = "breakout"

    def evaluate(self, symbol: str, win: RollingWindow, price: float) -> Signal | None:
        n = int(self.params.get("period", 20))
        if len(win) < n + 2:
            return None
        lo, _, hi = donchian(list(win.highs), list(win.lows), n)
        prev_hi = hi[-2]
        prev_lo = lo[-2]
        if np.isnan(prev_hi) or np.isnan(prev_lo):
            return None
        if price > prev_hi:
            return self._sig(symbol, SignalKind.BUY, 0.67, price, "Donchian 20 high break")
        if price < prev_lo:
            return self._sig(symbol, SignalKind.SELL, 0.67, price, "Donchian 20 low break")
        return None


class ZscoreRevert(Strategy):
    name = "zscore"
    title = "Z-score revert"
    family = "mean-reversion"

    def evaluate(self, symbol: str, win: RollingWindow, price: float) -> Signal | None:
        if len(win) < 25:
            return None
        z = zscore(list(win.closes), int(self.params.get("period", 20)))[-1]
        if np.isnan(z):
            return None
        if z <= -1.8:
            return self._sig(symbol, SignalKind.BUY, 0.6 + clamp((-z - 1.8) / 2), price, f"z-score {z:.2f}")
        if z >= 1.8:
            return self._sig(symbol, SignalKind.SELL, 0.6 + clamp((z - 1.8) / 2), price, f"z-score {z:.2f}")
        return None


class VolumeClimax(Strategy):
    name = "vol_climax"
    title = "Volume climax"
    family = "flow"

    def evaluate(self, symbol: str, win: RollingWindow, price: float) -> Signal | None:
        if len(win) < 25:
            return None
        vols = list(win.volumes)
        avg = float(np.mean(vols[-20:-1])) if len(vols) > 21 else 0
        last = vols[-1]
        if avg <= 0 or last < avg * 2.2:
            return None
        body = win.closes[-1] - win.opens[-1]
        if body > 0:
            return self._sig(symbol, SignalKind.BUY, 0.6, price, f"climax volume {last/avg:.1f}x green")
        return self._sig(symbol, SignalKind.SELL, 0.6, price, f"climax volume {last/avg:.1f}x red")


class RocMomentum(Strategy):
    name = "roc"
    title = "ROC thrust"
    family = "momentum"

    def evaluate(self, symbol: str, win: RollingWindow, price: float) -> Signal | None:
        n = int(self.params.get("period", 12))
        if len(win) < n + 5:
            return None
        series = roc(list(win.closes), n)
        val = series[-1]
        prev = series[-2]
        if np.isnan(val) or np.isnan(prev):
            return None
        if prev <= 0 < val:
            return self._sig(symbol, SignalKind.BUY, 0.58, price, f"ROC flipped + {val:.2f}")
        if prev >= 0 > val:
            return self._sig(symbol, SignalKind.SELL, 0.58, price, f"ROC flipped - {val:.2f}")
        return None


class CciReversion(Strategy):
    name = "cci"
    title = "CCI extremes"
    family = "mean-reversion"

    def evaluate(self, symbol: str, win: RollingWindow, price: float) -> Signal | None:
        if len(win) < 25:
            return None
        val = cci(list(win.highs), list(win.lows), list(win.closes))[-1]
        if np.isnan(val):
            return None
        if val <= -100:
            return self._sig(symbol, SignalKind.BUY, 0.57 + clamp((-val - 100) / 200), price, f"CCI {val:.0f}")
        if val >= 100:
            return self._sig(symbol, SignalKind.SELL, 0.57 + clamp((val - 100) / 200), price, f"CCI {val:.0f}")
        return None


class AtrChannel(Strategy):
    name = "atr_channel"
    title = "ATR channel"
    family = "volatility"

    def evaluate(self, symbol: str, win: RollingWindow, price: float) -> Signal | None:
        if len(win) < 30:
            return None
        a = atr(list(win.highs), list(win.lows), list(win.closes))[-1]
        mid = ema(list(win.closes), 21)[-1]
        if np.isnan(a) or np.isnan(mid) or a == 0:
            return None
        if price < mid - 1.6 * a:
            return self._sig(symbol, SignalKind.BUY, 0.56, price, "stretched below ATR channel")
        if price > mid + 1.6 * a:
            return self._sig(symbol, SignalKind.SELL, 0.56, price, "stretched above ATR channel")
        return None


REGISTRY = {
    "rsi_reversion": RsiReversion,
    "macd_trend": MacdTrend,
    "ema_trend": EmaTrend,
    "bollinger": BollingerReversion,
    "breakout": Breakout,
    "grid": GridHint,
    "supertrend": SupertrendFollow,
    "ichimoku": IchimokuKumo,
    "stoch_rsi": StochRsiCross,
    "vwap": VwapReversion,
    "adx_trend": AdxTrend,
    "keltner": KeltnerBreak,
    "donchian": DonchianTurtle,
    "zscore": ZscoreRevert,
    "vol_climax": VolumeClimax,
    "roc": RocMomentum,
    "cci": CciReversion,
    "atr_channel": AtrChannel,
}


def build_strategies(cfg: dict[str, dict[str, Any]]) -> list[Strategy]:
    out: list[Strategy] = []
    seen = set()
    for name, params in (cfg or {}).items():
        cls = REGISTRY.get(name)
        if not cls:
            continue
        out.append(cls(params or {}))
        seen.add(name)
    for name, cls in REGISTRY.items():
        if name in seen:
            continue
        out.append(cls({"enabled": False, "weight": 0.8}))
    return out


def ensemble(signals: list[Signal], min_confidence: float, min_votes: int = 2) -> Signal | None:
    actionable = [s for s in signals if s.kind in (SignalKind.BUY, SignalKind.SELL)]
    if not actionable:
        return None
    buys = [s for s in actionable if s.kind == SignalKind.BUY]
    sells = [s for s in actionable if s.kind == SignalKind.SELL]
    buy_score = sum(s.confidence for s in buys)
    sell_score = sum(s.confidence for s in sells)
    if buy_score == sell_score:
        return None
    if buy_score > sell_score:
        conf = buy_score / (buy_score + sell_score)
        best = max(buys, key=lambda s: s.confidence)
        kind = SignalKind.BUY
        votes = len(buys)
        reason = f"ensemble BUY {conf:.2f} ×{votes} — " + ", ".join(s.strategy for s in buys[:6])
    else:
        conf = sell_score / (buy_score + sell_score)
        best = max(sells, key=lambda s: s.confidence)
        kind = SignalKind.SELL
        votes = len(sells)
        reason = f"ensemble SELL {conf:.2f} ×{votes} — " + ", ".join(s.strategy for s in sells[:6])
    if votes < min_votes and best.confidence < 0.72:
        return None
    if conf < min_confidence:
        return None
    winners = buys if kind == SignalKind.BUY else sells
    extras: dict[str, Any] = {
        "buy_score": buy_score,
        "sell_score": sell_score,
        "votes": votes,
        "contributors": [s.strategy for s in winners[:8]],
    }
    # carry the leading signal's risk overrides (builder strategies set their own
    # stop / target / trail) so custom money management survives the vote
    for key in ("stop_loss_pct", "take_profit_pct", "trail_pct", "spec_id"):
        value = (best.extras or {}).get(key)
        if value:
            extras[key] = value
    return Signal(
        strategy="ensemble",
        symbol=best.symbol,
        kind=kind,
        confidence=conf,
        price=best.price,
        reason=reason,
        ts=time.time(),
        extras=extras,
    )
