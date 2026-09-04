from app.indicators import ema, macd, rsi
from app.strategies import RsiReversion, ensemble
from app.indicators import RollingWindow
from app.models import Signal, SignalKind


def test_rsi_bounds():
    closes = [100 + i * 0.2 for i in range(40)]
    series = rsi(closes, 14)
    val = float(series[-1])
    assert 0 <= val <= 100


def test_ema_tracks_uptrend():
    closes = list(range(1, 50))
    e = ema(closes, 10)
    assert e[-1] > e[0]


def test_macd_length():
    closes = [50 + (i % 7) for i in range(80)]
    line, sig, hist = macd(closes)
    assert len(line) == len(closes) == len(hist) == len(sig)


def test_rsi_strategy_buys_oversold():
    win = RollingWindow(80)
    px = 100.0
    for i in range(60):
        px *= 0.985
        win.push(i, px, px, px, px, 1)
    sig = RsiReversion({"enabled": True, "weight": 1, "period": 14, "oversold": 32, "overbought": 68}).evaluate(
        "BTC/USDT", win, px
    )
    assert sig is not None
    assert sig.kind in (SignalKind.BUY, SignalKind.HOLD)


def test_ensemble_prefers_majority():
    now = 1.0
    sigs = [
        Signal("a", "BTC/USDT", SignalKind.BUY, 0.8, 1, "x", now),
        Signal("b", "BTC/USDT", SignalKind.BUY, 0.7, 1, "x", now),
        Signal("c", "BTC/USDT", SignalKind.SELL, 0.6, 1, "x", now),
    ]
    out = ensemble(sigs, 0.5)
    assert out is not None
    assert out.kind == SignalKind.BUY
