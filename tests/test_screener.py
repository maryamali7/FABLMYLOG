from app.indicators import RollingWindow, rsi, stochastic, supertrend, zscore
from app.screener import build_boards, features
from app.models import Ticker
from app.strategies import SupertrendFollow, ZscoreRevert, ensemble
from app.models import Signal, SignalKind


def _win(n=80, start=100.0, drift=1.0):
    w = RollingWindow(200)
    px = start
    for i in range(n):
        px *= drift
        w.push(i, px, px * 1.002, px * 0.998, px, 10 + i)
    return w, px


def test_zscore_mean_and_extreme():
    w, px = _win(60, 50, 0.97)
    z = zscore(list(w.closes), 20)
    assert z[-1] < 0


def test_supertrend_has_direction():
    w, px = _win(80, 100, 1.004)
    st, d = supertrend(list(w.highs), list(w.lows), list(w.closes))
    assert len(d) == len(w)
    assert d[-1] in (1, -1)


def test_stoch_bounds():
    w, px = _win(50, 80, 1.001)
    k, d = stochastic(list(w.highs), list(w.lows), list(w.closes))
    assert 0 <= k[-1] <= 100


def test_features_alpha():
    w, px = _win(80, 100, 1.003)
    t = Ticker("sim", "AAA/USDT", px, px, px, 1e6, 1.0, change_pct=4.2)
    row = features("AAA/USDT", w, t, 1.0)
    assert row is not None
    assert 0 <= row["alpha"] <= 100
    assert "rsi" in row


def test_boards_rank_gainers():
    rows = [
        {"symbol": "A", "alpha": 80, "change_pct": 9, "rsi": 70, "zscore": 2, "vol_ratio": 3, "adx": 30, "bb_width": 1, "squeeze": False, "breakout": True, "roc": 2, "trend": "up", "rs_btc": 4, "atr_pct": 1},
        {"symbol": "B", "alpha": 20, "change_pct": -8, "rsi": 22, "zscore": -2, "vol_ratio": 1, "adx": 10, "bb_width": 0.5, "squeeze": True, "breakout": False, "roc": -2, "trend": "down", "rs_btc": -5, "atr_pct": 2},
    ]
    boards = build_boards(rows)
    assert boards["gainers"][0]["symbol"] == "A"
    assert boards["losers"][0]["symbol"] == "B"


def test_zscore_strategy_buys_dump():
    w, px = _win(40, 100, 1.0)
    for i in range(3):
        px *= 0.8
        w.push(40 + i, px, px, px, px, 12)
    sig = ZscoreRevert({"enabled": True, "weight": 1, "period": 20}).evaluate("BTC/USDT", w, px)
    assert sig is not None
    assert sig.kind == SignalKind.BUY


def test_supertrend_strategy_runs():
    w, px = _win(60, 90, 1.006)
    sig = SupertrendFollow({"enabled": True, "weight": 1}).evaluate("ETH/USDT", w, px)
    assert sig is not None


def test_rsi_series_still_ok():
    w, px = _win(40, 100, 1.0)
    series = rsi(list(w.closes), 14)
    assert 0 <= float(series[-1]) <= 100


def test_ensemble_needs_confluence():
    now = 1.0
    weak = [Signal("a", "BTC/USDT", SignalKind.BUY, 0.4, 1, "x", now)]
    assert ensemble(weak, 0.5, min_votes=2) is None
    strong = [
        Signal("a", "BTC/USDT", SignalKind.BUY, 0.8, 1, "x", now),
        Signal("b", "BTC/USDT", SignalKind.BUY, 0.7, 1, "x", now),
    ]
    out = ensemble(strong, 0.5, min_votes=2)
    assert out.kind == SignalKind.BUY
