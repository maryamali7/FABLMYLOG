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


# --------------------------------------------------------------------------- #
# advanced screener: query engine, presets, export, summary
# --------------------------------------------------------------------------- #

from app.screener import (
    BOARD_KEYS,
    DEFAULT_COLUMNS,
    PRESETS,
    build_boards,
    preset,
    rows_to_csv,
    run_query,
    summarize,
)


def _rows():
    return [
        {
            "symbol": "AAA/USDT", "alpha": 82, "quality": 74, "risk_score": 30, "liquidity": 80,
            "change_pct": 6.5, "rsi": 64, "adx": 31, "vol_ratio": 2.4, "atr_pct": 0.7, "zscore": 1.8,
            "trend": "up", "supertrend": "bull", "bias": "long", "grade": "A", "signal_count": 8,
            "rs_btc": 3.2, "corr_btc": 0.4, "squeeze": False, "breakout": True, "bb_width": 3.1,
            "trend_score": 78, "mom_score": 71, "roc": 4.1, "bear_count": 2, "dist_hh_pct": 0.2,
            "vwap_dev": 0.4, "macd_hist": 0.9, "macd": 2.0, "bb_pct": 0.9, "last": 12.5, "volume": 4e7,
        },
        {
            "symbol": "BBB/USDT", "alpha": 24, "quality": 31, "risk_score": 72, "liquidity": 40,
            "change_pct": -7.2, "rsi": 26, "adx": 12, "vol_ratio": 0.8, "atr_pct": 1.9, "zscore": -2.2,
            "trend": "down", "supertrend": "bear", "bias": "short", "grade": "D", "signal_count": 2,
            "rs_btc": -5.4, "corr_btc": 0.8, "squeeze": True, "breakout": False, "bb_width": 1.2,
            "trend_score": 28, "mom_score": 33, "roc": -3.8, "bear_count": 8, "dist_hh_pct": 9.4,
            "vwap_dev": -1.9, "macd_hist": -0.5, "macd": -1.1, "bb_pct": 0.05, "last": 0.42, "volume": 9e5,
        },
        {
            "symbol": "CCC/USDT", "alpha": 57, "quality": 55, "risk_score": 44, "liquidity": 66,
            "change_pct": 0.4, "rsi": 51, "adx": 23, "vol_ratio": 1.4, "atr_pct": 0.5, "zscore": 0.2,
            "trend": "up", "supertrend": "bull", "bias": "neutral", "grade": "B", "signal_count": 6,
            "rs_btc": 0.6, "corr_btc": 0.6, "squeeze": True, "breakout": False, "bb_width": 1.6,
            "trend_score": 60, "mom_score": 56, "roc": 0.8, "bear_count": 4, "dist_hh_pct": 1.1,
            "vwap_dev": 0.2, "macd_hist": 0.1, "macd": 0.5, "bb_pct": 0.55, "last": 3.1, "volume": 8e6,
        },
    ]


def test_query_filters_and_sorts():
    res = run_query(_rows(), filters=[{"left": "alpha", "cmp": ">", "right": 50}], sort_by="alpha")
    assert [r["symbol"] for r in res["rows"]] == ["AAA/USDT", "CCC/USDT"]
    assert res["total"] == 2
    asc = run_query(_rows(), sort_by="alpha", sort_dir="asc")
    assert asc["rows"][0]["symbol"] == "BBB/USDT"


def test_query_match_any_and_none():
    filters = [{"left": "rsi", "cmp": ">", "right": 60}, {"left": "rsi", "cmp": "<", "right": 30}]
    any_hits = run_query(_rows(), filters=filters, match="any")
    assert {r["symbol"] for r in any_hits["rows"]} == {"AAA/USDT", "BBB/USDT"}
    none_hits = run_query(_rows(), filters=filters, match="none")
    assert [r["symbol"] for r in none_hits["rows"]] == ["CCC/USDT"]


def test_query_string_filters_and_search():
    res = run_query(_rows(), filters=[{"left": "grade", "cmp": "==", "right": "A"}])
    assert res["total"] == 1 and res["rows"][0]["symbol"] == "AAA/USDT"
    res = run_query(_rows(), search="ccc")
    assert res["total"] == 1


def test_query_limit_and_bad_filter_is_safe():
    res = run_query(_rows(), limit=1)
    assert res["returned"] == 1 and res["total"] == 3
    broken = run_query(_rows(), filters=[{"left": "not_a_field", "cmp": ">", "right": 1}])
    assert broken["total"] == 0


def test_presets_are_runnable():
    rows = _rows()
    assert preset("nope") is None
    for p in PRESETS:
        out = run_query(rows, preset_id=p["id"])
        assert out["total"] <= len(rows)
        assert out["sort_by"] == p["sort"]


def test_new_boards_exist_and_populate():
    boards = build_boards(_rows())
    for key in BOARD_KEYS:
        assert key in boards
    assert boards["confluence"][0]["symbol"] == "AAA/USDT"
    assert boards["quality"][0]["symbol"] == "AAA/USDT"
    assert [r["symbol"] for r in boards["short_setups"]] == []


def test_summary_and_csv_export():
    rows = _rows()
    s = summarize(rows)
    assert s["n"] == 3 and s["advancers"] == 2 and s["grade_a"] == 1
    csv_text = rows_to_csv(rows)
    header = csv_text.splitlines()[0].split(",")
    assert header == DEFAULT_COLUMNS
    assert "AAA/USDT" in csv_text
    assert len(csv_text.strip().splitlines()) == 4


def test_features_include_advanced_factors():
    w, px = _win(120, 100, 1.003)
    t = Ticker("sim", "AAA/USDT", px, px * 0.999, px * 1.001, 5e6, 1.0, change_pct=4.2)
    row = features("AAA/USDT", w, t, 1.0)
    for key in ("quality", "risk_score", "liquidity", "signal_count", "grade", "trend_score", "mom_score"):
        assert key in row
    assert row["grade"] in ("A", "B", "C", "D")
    assert 0 <= row["quality"] <= 100
    assert 0 <= row["risk_score"] <= 100
    assert isinstance(row["confluence"], list)
