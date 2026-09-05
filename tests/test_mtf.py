"""Tests for the multi-timeframe engine and the next-move forecast ensemble."""

from __future__ import annotations

import asyncio
import math
import random
import time

import pytest

from app.indicators import RollingWindow
from app.predict import HORIZONS, levels, predict, rank_forecasts, regime
from app.rules import ALL_FIELDS, compute_frame
from app.timeframes import (
    MIN_TF_BARS,
    TF_ORDER,
    TF_SECONDS,
    MTFEngine,
    align,
    rating_label,
    resample,
    rsi_state,
    tf_metrics,
)


def make_window(bars: int = 2000, drift: float = 0.00008, seed: int = 7) -> RollingWindow:
    rng = random.Random(seed)
    win = RollingWindow(bars + 50)
    price = 100.0
    start = time.time() - bars * 60
    for i in range(bars):
        price *= math.exp(rng.gauss(drift, 0.0025))
        hi = price * (1 + abs(rng.gauss(0, 0.0009)))
        lo = price * (1 - abs(rng.gauss(0, 0.0009)))
        win.push(start + i * 60, price, hi, lo, price, abs(rng.gauss(1000, 120)))
    return win


class FakeHub:
    def __init__(self, windows):
        self.candles = dict(windows)
        self.books = {}

    def quote(self, symbol):  # pragma: no cover - trivial
        return None


# --------------------------------------------------------------------------- #
# timeframes
# --------------------------------------------------------------------------- #


def test_rsi_state_buckets():
    assert rsi_state(82) == "overbought"
    assert rsi_state(64) == "bullish"
    assert rsi_state(50) == "neutral"
    assert rsi_state(35) == "bearish"
    assert rsi_state(12) == "oversold"


def test_rating_label_scale():
    assert rating_label(70) == "strong buy"
    assert rating_label(25) == "buy"
    assert rating_label(0) == "neutral"
    assert rating_label(-25) == "sell"
    assert rating_label(-70) == "strong sell"


def test_resample_buckets_are_aligned_and_ohlc_consistent():
    win = make_window(600)
    rows = resample(win, TF_SECONDS["15m"])
    assert 35 <= len(rows) <= 41
    for row in rows:
        assert row["ts"] % 900 == 0
        assert row["high"] >= max(row["open"], row["close"]) - 1e-9
        assert row["low"] <= min(row["open"], row["close"]) + 1e-9
        assert row["volume"] > 0
    # buckets ascend and never repeat
    stamps = [r["ts"] for r in rows]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps)


def test_resample_accepts_plain_rows():
    win = make_window(300)
    raw = [
        {"ts": win.ts[i], "open": win.opens[i], "high": win.highs[i], "low": win.lows[i],
         "close": win.closes[i], "volume": win.volumes[i]}
        for i in range(len(win))
    ]
    assert resample(raw, TF_SECONDS["5m"]) == resample(win, TF_SECONDS["5m"])


def test_tf_metrics_reports_full_indicator_set():
    win = make_window(1200)
    rows = resample(win, TF_SECONDS["5m"])
    m = tf_metrics("5m", rows, "resample")
    assert m["available"] is True
    assert m["tf"] == "5m" and m["source"] == "resample"
    for key in ("rsi", "rsi_state", "adx", "macd_state", "trend", "score", "rating", "atr_pct", "bars"):
        assert key in m
    assert 0 <= m["rsi"] <= 100
    assert -100 <= m["score"] <= 100
    assert m["rsi_state"] == rsi_state(m["rsi"])
    assert m["rating"] == rating_label(m["score"])


def test_tf_metrics_flags_thin_history():
    win = make_window(40)
    m = tf_metrics("1h", resample(win, TF_SECONDS["1h"]), "resample")
    assert m["available"] is False
    assert m["bars"] < MIN_TF_BARS
    assert m["reason"]


def test_align_scores_agreement_and_conflicts():
    rows = [
        {"tf": "1m", "available": True, "score": 60, "rsi_state": "overbought"},
        {"tf": "15m", "available": True, "score": 40, "rsi_state": "bullish"},
        {"tf": "1h", "available": True, "score": 55, "rsi_state": "neutral"},
    ]
    out = align(rows)
    assert out["bias"] == "long"
    assert out["verdict"] in ("uptrend", "strong uptrend")
    assert out["bulls"] == 3 and out["bears"] == 0
    assert out["agreement"] == 100.0
    assert out["conflicts"] == ["1m overbought"]

    mixed = align(
        [
            {"tf": "1m", "available": True, "score": 55, "rsi_state": "neutral"},
            {"tf": "1h", "available": True, "score": -55, "rsi_state": "neutral"},
        ]
    )
    assert mixed["agreement"] == 50.0
    # the 1h frame outweighs the 1m frame, so the tie breaks bearish
    assert mixed["bias"] == "short"


def test_align_without_usable_frames():
    out = align([{"tf": "1d", "available": False}])
    assert out["timeframes"] == 0 and out["verdict"] == "unknown"


def test_engine_refresh_and_flat_fields_offline():
    win = make_window(2400)
    hub = FakeHub({"BTC/USDT": win})
    eng = MTFEngine(hub, None)
    eng.rest_ok = False
    asyncio.run(eng.refresh_symbol("BTC/USDT", force=True))

    snap = eng.snapshot("BTC/USDT")
    assert snap["ready"] is True
    available = [r["tf"] for r in snap["timeframes"] if r["available"]]
    assert {"1m", "5m", "15m"} <= set(available)
    assert [r["tf"] for r in snap["timeframes"]] == TF_ORDER[: len(snap["timeframes"])]

    flat = eng.flat_fields("BTC/USDT")
    assert flat["mtf_timeframes"] == len(available)
    assert "rsi_15m" in flat and "trend_15m" in flat and "adx_15m" in flat
    # every flattened key must be screenable / usable in the rule builder
    for key in flat:
        assert key in ALL_FIELDS, key


def test_engine_round_robin_and_scan():
    hub = FakeHub({"BTC/USDT": make_window(900, seed=1), "ETH/USDT": make_window(900, seed=2)})
    eng = MTFEngine(hub, None)
    eng.rest_ok = False
    first = asyncio.run(eng.refresh_next(["BTC/USDT", "ETH/USDT"], batch=1))
    second = asyncio.run(eng.refresh_next(["BTC/USDT", "ETH/USDT"], batch=1))
    assert first != second  # cursor advanced

    rows = eng.scan(["BTC/USDT", "ETH/USDT"])
    assert len(rows) == 2
    assert rows[0]["mtf_score"] >= rows[1]["mtf_score"]
    for row in rows:
        assert "agreement" in row and "verdict" in row


def test_engine_best_frame_prefers_cached_rows():
    hub = FakeHub({"BTC/USDT": make_window(1500)})
    eng = MTFEngine(hub, None)
    eng.rest_ok = False
    asyncio.run(eng.refresh_symbol("BTC/USDT", force=True))
    rows = eng.best_frame("BTC/USDT", "15m")
    assert rows and rows[0]["ts"] % 900 == 0


# --------------------------------------------------------------------------- #
# forecasting
# --------------------------------------------------------------------------- #


def test_levels_cluster_and_sort_around_price():
    win = make_window(800)
    frame = compute_frame(win)
    out = levels(frame)
    price = out["price"]
    assert all(l["price"] < price for l in out["support"])
    assert all(l["price"] > price for l in out["resistance"])
    assert out["support"] == sorted(out["support"], key=lambda l: -l["price"])
    assert out["resistance"] == sorted(out["resistance"], key=lambda l: l["price"])
    if out["nearest_support"]:
        assert out["nearest_support"]["distance_pct"] <= 0
    if out["nearest_resistance"]:
        assert out["nearest_resistance"]["distance_pct"] >= 0


def test_regime_classifies_known_states():
    frame = compute_frame(make_window(700))
    from app.rules import context_at

    ctx = context_at(frame, -1)
    reg = regime(ctx)
    assert reg["name"] in ("trending", "volatile", "quiet", "transitional", "ranging")
    assert reg["detail"]


def test_predict_shape_and_bounds():
    win = make_window(1200, drift=0.0004)
    out = predict(win, symbol="BTC/USDT", timeframe="1m")
    assert out["ok"] is True
    assert out["symbol"] == "BTC/USDT" and out["timeframe"] == "1m"
    assert out["horizon_bars"] == HORIZONS["1m"]
    assert out["direction"] in ("up", "down", "flat")
    assert 0 <= out["probability_up"] <= 100
    assert abs(out["probability_up"] + out["probability_down"] - 100) < 0.2
    assert out["lower"] <= out["target"] <= out["upper"]
    assert 0 <= out["confidence"] <= 100
    assert len(out["path"]) == out["horizon_bars"]
    assert out["path"][-1]["upper"] > out["path"][-1]["lower"]
    assert len(out["models"]) >= 5
    assert out["rationale"]
    assert len(out["history"]) <= 60
    for m in out["models"]:
        assert -1.0001 <= m["score"] <= 1.0001
        assert 0 <= m["confidence"] <= 1.0001


def test_predict_uptrend_leans_up():
    strong = RollingWindow(600)
    start = time.time() - 400 * 60
    price = 100.0
    for i in range(400):
        price *= 1.0025
        strong.push(start + i * 60, price, price * 1.001, price * 0.999, price, 1000.0)
    out = predict(strong, symbol="UP/USDT", timeframe="1m")
    assert out["direction"] == "up"
    assert out["probability_up"] > 50
    assert out["expected_move_pct"] > 0


def test_predict_needs_history():
    tiny = make_window(20)
    out = predict(tiny, symbol="X/USDT", timeframe="1m")
    assert out["ok"] is False and out["error"]


def test_predict_uses_mtf_context():
    win = make_window(900)
    hub = FakeHub({"BTC/USDT": win})
    eng = MTFEngine(hub, None)
    eng.rest_ok = False
    asyncio.run(eng.refresh_symbol("BTC/USDT", force=True))
    out = predict(win, symbol="BTC/USDT", timeframe="1m", mtf=eng.snapshot("BTC/USDT"))
    assert any("Multi-timeframe" in line for line in out["rationale"])


@pytest.mark.parametrize("tf", ["5m", "15m", "1h"])
def test_predict_per_timeframe_horizons(tf):
    win = make_window(4200)
    rows = resample(win, TF_SECONDS[tf])
    out = predict(rows, symbol="BTC/USDT", timeframe=tf)
    assert out["ok"] is True
    assert out["horizon_bars"] == HORIZONS[tf]
    assert tf in out["horizon_label"] or out["horizon_label"]


def test_rank_forecasts_splits_and_sorts():
    made = []
    for i, drift in enumerate((0.0006, -0.0006, 0.0001)):
        w = make_window(500, drift=drift, seed=10 + i)
        made.append(predict(w, symbol=f"S{i}/USDT", timeframe="1m"))
    board = rank_forecasts(made)
    assert set(board) == {"up", "down", "all"}
    edges = [r["edge"] for r in board["all"]]
    assert edges == sorted(edges, reverse=True)
    for row in board["up"]:
        assert row["direction"] == "up"
    for row in board["down"]:
        assert row["direction"] == "down"
    assert all("top_reason" in r for r in board["all"])


def test_predict_is_fast_enough_for_the_loop():
    win = make_window(2000)
    started = time.time()
    predict(win, symbol="BTC/USDT", timeframe="1m")
    assert (time.time() - started) < 0.35
