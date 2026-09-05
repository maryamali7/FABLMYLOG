from __future__ import annotations

import math
from collections import deque
from itertools import islice

import numpy as np


def ema(values: list[float] | np.ndarray, period: int) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    if len(arr) == 0:
        return arr
    alpha = 2.0 / (period + 1)
    out = np.empty_like(arr)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]
    return out


def sma(values: list[float] | np.ndarray, period: int) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    if len(arr) < period:
        return np.full(len(arr), np.nan)
    csum = np.cumsum(arr)
    out = np.full(len(arr), np.nan)
    out[period - 1] = csum[period - 1] / period
    for i in range(period, len(arr)):
        out[i] = (csum[i] - csum[i - period]) / period
    return out


def rsi(closes: list[float] | np.ndarray, period: int = 14) -> np.ndarray:
    arr = np.asarray(closes, dtype=float)
    n = len(arr)
    out = np.full(n, np.nan)
    if n < period + 1:
        return out
    deltas = np.diff(arr)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    if avg_loss == 0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100 - (100 / (1 + rs))
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            out[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i + 1] = 100 - (100 / (1 + rs))
    return out


def macd(closes: list[float] | np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9):
    c = np.asarray(closes, dtype=float)
    macd_line = ema(c, fast) - ema(c, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def bollinger(closes: list[float] | np.ndarray, period: int = 20, stdev: float = 2.0):
    arr = np.asarray(closes, dtype=float)
    mid = sma(arr, period)
    out_u = np.full(len(arr), np.nan)
    out_l = np.full(len(arr), np.nan)
    for i in range(period - 1, len(arr)):
        window = arr[i - period + 1 : i + 1]
        sd = float(np.std(window, ddof=0))
        out_u[i] = mid[i] + stdev * sd
        out_l[i] = mid[i] - stdev * sd
    return out_l, mid, out_u


def atr(highs, lows, closes, period: int = 14) -> np.ndarray:
    h = np.asarray(highs, dtype=float)
    l = np.asarray(lows, dtype=float)
    c = np.asarray(closes, dtype=float)
    n = len(c)
    tr = np.zeros(n)
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    return ema(tr, period)


def slope(values: list[float] | np.ndarray, lookback: int = 5) -> float:
    arr = np.asarray(values, dtype=float)
    if len(arr) < lookback:
        return 0.0
    y = arr[-lookback:]
    x = np.arange(len(y))
    if np.allclose(y, y[0]):
        return 0.0
    denom = (len(y) * np.sum(x * x) - np.sum(x) ** 2)
    if denom == 0:
        return 0.0
    m = (len(y) * np.sum(x * y) - np.sum(x) * np.sum(y)) / denom
    return float(m / (abs(y[-1]) + 1e-12))


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def roc(closes: list[float] | np.ndarray, period: int = 12) -> np.ndarray:
    arr = np.asarray(closes, dtype=float)
    out = np.full(len(arr), np.nan)
    for i in range(period, len(arr)):
        if arr[i - period] == 0:
            continue
        out[i] = (arr[i] / arr[i - period] - 1.0) * 100
    return out


def zscore(closes: list[float] | np.ndarray, period: int = 20) -> np.ndarray:
    arr = np.asarray(closes, dtype=float)
    out = np.full(len(arr), np.nan)
    for i in range(period - 1, len(arr)):
        w = arr[i - period + 1 : i + 1]
        sd = float(np.std(w))
        if sd == 0:
            out[i] = 0.0
        else:
            out[i] = (arr[i] - float(np.mean(w))) / sd
    return out


def stochastic(highs, lows, closes, k: int = 14, d: int = 3):
    h = np.asarray(highs, dtype=float)
    l = np.asarray(lows, dtype=float)
    c = np.asarray(closes, dtype=float)
    n = len(c)
    raw = np.full(n, np.nan)
    for i in range(k - 1, n):
        hh = np.max(h[i - k + 1 : i + 1])
        ll = np.min(l[i - k + 1 : i + 1])
        raw[i] = 50.0 if hh == ll else (c[i] - ll) / (hh - ll) * 100
    k_line = ema(np.nan_to_num(raw, nan=50.0), 3)
    d_line = sma(k_line, d)
    return k_line, d_line


def stoch_rsi(closes, period: int = 14, k: int = 3, d: int = 3):
    r = rsi(closes, period)
    n = len(r)
    raw = np.full(n, np.nan)
    for i in range(period * 2, n):
        w = r[i - period + 1 : i + 1]
        lo, hi = np.nanmin(w), np.nanmax(w)
        if np.isnan(r[i]) or hi == lo:
            raw[i] = 50.0
        else:
            raw[i] = (r[i] - lo) / (hi - lo) * 100
    filled = np.nan_to_num(raw, nan=50.0)
    k_line = sma(filled, k)
    d_line = sma(k_line, d)
    return k_line, d_line


def cci(highs, lows, closes, period: int = 20) -> np.ndarray:
    h = np.asarray(highs, dtype=float)
    l = np.asarray(lows, dtype=float)
    c = np.asarray(closes, dtype=float)
    tp = (h + l + c) / 3.0
    mid = sma(tp, period)
    out = np.full(len(c), np.nan)
    for i in range(period - 1, len(c)):
        w = tp[i - period + 1 : i + 1]
        md = float(np.mean(np.abs(w - mid[i])))
        out[i] = 0.0 if md == 0 else (tp[i] - mid[i]) / (0.015 * md)
    return out


def williams_r(highs, lows, closes, period: int = 14) -> np.ndarray:
    h = np.asarray(highs, dtype=float)
    l = np.asarray(lows, dtype=float)
    c = np.asarray(closes, dtype=float)
    out = np.full(len(c), np.nan)
    for i in range(period - 1, len(c)):
        hh = np.max(h[i - period + 1 : i + 1])
        ll = np.min(l[i - period + 1 : i + 1])
        out[i] = -50.0 if hh == ll else (hh - c[i]) / (hh - ll) * -100
    return out


def obv(closes, volumes) -> np.ndarray:
    c = np.asarray(closes, dtype=float)
    v = np.asarray(volumes, dtype=float)
    out = np.zeros(len(c))
    for i in range(1, len(c)):
        if c[i] > c[i - 1]:
            out[i] = out[i - 1] + v[i]
        elif c[i] < c[i - 1]:
            out[i] = out[i - 1] - v[i]
        else:
            out[i] = out[i - 1]
    return out


def vwap(highs, lows, closes, volumes) -> np.ndarray:
    tp = (np.asarray(highs) + np.asarray(lows) + np.asarray(closes)) / 3.0
    vol = np.asarray(volumes, dtype=float)
    pv = np.cumsum(tp * vol)
    cv = np.cumsum(vol)
    out = np.full(len(tp), np.nan)
    np.divide(pv, cv, out=out, where=cv > 0)
    return out


def donchian(highs, lows, period: int = 20):
    h = np.asarray(highs, dtype=float)
    l = np.asarray(lows, dtype=float)
    up = np.full(len(h), np.nan)
    dn = np.full(len(l), np.nan)
    for i in range(period - 1, len(h)):
        up[i] = np.max(h[i - period + 1 : i + 1])
        dn[i] = np.min(l[i - period + 1 : i + 1])
    mid = (up + dn) / 2.0
    return dn, mid, up


def keltner(highs, lows, closes, period: int = 20, atr_mult: float = 1.5):
    mid = ema(closes, period)
    a = atr(highs, lows, closes, period)
    return mid - atr_mult * a, mid, mid + atr_mult * a


def adx(highs, lows, closes, period: int = 14):
    h = np.asarray(highs, dtype=float)
    l = np.asarray(lows, dtype=float)
    c = np.asarray(closes, dtype=float)
    n = len(c)
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    tr = np.zeros(n)
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        up = h[i] - h[i - 1]
        dn = l[i - 1] - l[i]
        plus_dm[i] = up if up > dn and up > 0 else 0.0
        minus_dm[i] = dn if dn > up and dn > 0 else 0.0
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    atr_s = ema(tr, period)
    pdi = np.zeros(n)
    mdi = np.zeros(n)
    dx = np.zeros(n)
    pdm = ema(plus_dm, period)
    mdm = ema(minus_dm, period)
    for i in range(n):
        if atr_s[i] == 0:
            continue
        pdi[i] = 100 * pdm[i] / atr_s[i]
        mdi[i] = 100 * mdm[i] / atr_s[i]
        s = pdi[i] + mdi[i]
        dx[i] = 0.0 if s == 0 else 100 * abs(pdi[i] - mdi[i]) / s
    return ema(dx, period), pdi, mdi


def supertrend(highs, lows, closes, period: int = 10, mult: float = 3.0):
    h = np.asarray(highs, dtype=float)
    l = np.asarray(lows, dtype=float)
    c = np.asarray(closes, dtype=float)
    a = atr(h, l, c, period)
    hl2 = (h + l) / 2.0
    upper = hl2 + mult * a
    lower = hl2 - mult * a
    st = np.copy(hl2)
    direction = np.ones(len(c))
    for i in range(1, len(c)):
        lower[i] = max(lower[i], lower[i - 1]) if c[i - 1] > lower[i - 1] else lower[i]
        upper[i] = min(upper[i], upper[i - 1]) if c[i - 1] < upper[i - 1] else upper[i]
        if st[i - 1] == upper[i - 1]:
            direction[i] = -1 if c[i] <= upper[i] else 1
        else:
            direction[i] = 1 if c[i] >= lower[i] else -1
        st[i] = lower[i] if direction[i] == 1 else upper[i]
    return st, direction


def ichimoku(highs, lows, closes, tenkan: int = 9, kijun: int = 26, senkou: int = 52):
    h = np.asarray(highs, dtype=float)
    l = np.asarray(lows, dtype=float)
    c = np.asarray(closes, dtype=float)

    def mid(period, i):
        return (np.max(h[i - period + 1 : i + 1]) + np.min(l[i - period + 1 : i + 1])) / 2.0

    n = len(c)
    t = np.full(n, np.nan)
    k = np.full(n, np.nan)
    sa = np.full(n, np.nan)
    sb = np.full(n, np.nan)
    for i in range(n):
        if i >= tenkan - 1:
            t[i] = mid(tenkan, i)
        if i >= kijun - 1:
            k[i] = mid(kijun, i)
        if i >= kijun - 1 and not np.isnan(t[i]) and not np.isnan(k[i]):
            sa[i] = (t[i] + k[i]) / 2.0
        if i >= senkou - 1:
            sb[i] = mid(senkou, i)
    return t, k, sa, sb


def hv(closes, period: int = 20) -> float:
    arr = np.asarray(closes, dtype=float)
    if len(arr) < period + 1:
        return 0.0
    rets = np.diff(np.log(np.clip(arr[-period - 1 :], 1e-12, None)))
    return float(np.std(rets) * math.sqrt(365 * 24 * 60))


def last_valid(arr) -> float | None:
    a = np.asarray(arr, dtype=float)
    if len(a) == 0:
        return None
    for x in a[::-1]:
        if not np.isnan(x):
            return float(x)
    return None


class RollingWindow:
    def __init__(self, maxlen: int = 500):
        self.opens: deque[float] = deque(maxlen=maxlen)
        self.highs: deque[float] = deque(maxlen=maxlen)
        self.lows: deque[float] = deque(maxlen=maxlen)
        self.closes: deque[float] = deque(maxlen=maxlen)
        self.volumes: deque[float] = deque(maxlen=maxlen)
        self.ts: deque[float] = deque(maxlen=maxlen)

    def push(self, ts: float, o: float, h: float, l: float, c: float, v: float) -> None:
        if self.ts and ts == self.ts[-1]:
            self.opens[-1] = o
            self.highs[-1] = h
            self.lows[-1] = l
            self.closes[-1] = c
            self.volumes[-1] = v
            return
        self.ts.append(ts)
        self.opens.append(o)
        self.highs.append(h)
        self.lows.append(l)
        self.closes.append(c)
        self.volumes.append(v)

    def tick(self, price: float, ts: float, vol: float = 0.0) -> None:
        if not self.closes:
            self.push(ts, price, price, price, price, vol)
            return
        bucket = math.floor(ts / 60) * 60
        last_bucket = math.floor(self.ts[-1] / 60) * 60
        if bucket == last_bucket:
            self.highs[-1] = max(self.highs[-1], price)
            self.lows[-1] = min(self.lows[-1], price)
            self.closes[-1] = price
            self.volumes[-1] += vol
            self.ts[-1] = ts
        else:
            self.push(ts, price, price, price, price, vol)

    def tail(self, n: int) -> "RollingWindow":
        """Cheap bounded view of the last ``n`` bars.

        The hub keeps days of 1m history for multi-timeframe resampling, but the
        strategy/indicator layer only ever needs a few hundred bars — feeding it
        the whole window turns the pure-Python indicator loops into a bottleneck.
        """
        if n <= 0 or len(self.closes) <= n:
            return self
        out = RollingWindow(n)
        out.ts = deque(islice(self.ts, len(self.ts) - n, len(self.ts)), maxlen=n)
        out.opens = deque(islice(self.opens, len(self.opens) - n, len(self.opens)), maxlen=n)
        out.highs = deque(islice(self.highs, len(self.highs) - n, len(self.highs)), maxlen=n)
        out.lows = deque(islice(self.lows, len(self.lows) - n, len(self.lows)), maxlen=n)
        out.closes = deque(islice(self.closes, len(self.closes) - n, len(self.closes)), maxlen=n)
        out.volumes = deque(islice(self.volumes, len(self.volumes) - n, len(self.volumes)), maxlen=n)
        return out

    def __len__(self) -> int:
        return len(self.closes)
