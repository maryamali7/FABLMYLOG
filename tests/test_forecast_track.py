"""Forecast scoreboard + replay-safe multi-timeframe context tests."""

from __future__ import annotations

import math
import random
import time

from app.backtest import backtest, spec_fields
from app.forecast_track import ForecastTracker, horizon_seconds
from app.indicators import RollingWindow
from app.timeframes import mtf_history


# Fixed, hour-aligned epoch: higher-timeframe buckets are cut on wall-clock
# boundaries, so a wall-clock start makes resampling tests depend on the hour
# they happen to run in.
EPOCH = 1_700_000_000 - (1_700_000_000 % 3600)


def window(bars: int = 2500, drift: float = 0.00012, seed: int = 5) -> RollingWindow:
    rng = random.Random(seed)
    win = RollingWindow(bars + 50)
    price = 100.0
    start = EPOCH - bars * 60
    for i in range(bars):
        price *= math.exp(rng.gauss(drift, 0.003))
        win.push(start + i * 60, price, price * 1.001, price * 0.999, price, 1000.0)
    return win


def rows_of(win: RollingWindow) -> list[dict[str, float]]:
    return [
        {"ts": win.ts[i], "open": win.opens[i], "high": win.highs[i], "low": win.lows[i],
         "close": win.closes[i], "volume": win.volumes[i]}
        for i in range(len(win))
    ]


def forecast(symbol="BTC/USDT", tf="1m", direction="up", prob=64.0, move=0.5, price=100.0, bars=15):
    return {
        "ok": True,
        "symbol": symbol,
        "timeframe": tf,
        "price": price,
        "direction": direction,
        "probability_up": prob,
        "expected_move_pct": move,
        "confidence": 60.0,
        "target": price * (1 + move / 100),
        "upper": price * 1.02,
        "lower": price * 0.98,
        "horizon_bars": bars,
        "horizon_label": f"next {bars} min",
        "regime": {"name": "trending"},
        "models": [
            {"name": "Trend", "score": 0.6},
            {"name": "Mean reversion", "score": -0.4},
            {"name": "Flat model", "score": 0.0},
        ],
    }


# --------------------------------------------------------------------------- #
# tracker
# --------------------------------------------------------------------------- #


def test_horizon_seconds_uses_timeframe():
    assert horizon_seconds("1m", 15) == 900
    assert horizon_seconds("1h", 6) == 6 * 3600


def test_record_dedupes_then_settles_a_winner(tmp_path):
    t = ForecastTracker(tmp_path / "log.json", min_gap_sec=120)
    now = time.time()
    assert t.record(forecast(price=100.0), now=now) is True
    assert t.record(forecast(price=100.5), now=now + 10) is False  # too soon
    assert len(t.open) == 1

    # not mature yet
    assert t.settle(lambda s: 101.0, now=now + 60) == []
    done = t.settle(lambda s: 101.0, now=now + 901)
    assert len(done) == 1
    row = done[0]
    assert row["hit"] is True
    assert row["actual_move_pct"] > 0
    assert row["in_band"] is True
    assert row["brier"] < 0.25
    assert not t.open and len(t.settled) == 1


def test_settle_marks_a_miss_and_scores_models(tmp_path):
    t = ForecastTracker(tmp_path / "log.json")
    now = time.time()
    t.record(forecast(price=100.0, direction="up", prob=70.0), now=now)
    row = t.settle(lambda s: 97.0, now=now + 1000)[0]
    assert row["hit"] is False
    assert row["brier"] > 0.25
    assert row["in_band"] is False
    named = {m["name"]: m["hit"] for m in row["models"]}
    assert named["Trend"] is False  # bullish model, price fell
    assert named["Mean reversion"] is True
    assert named["Flat model"] is None  # no opinion, not scored


def test_flat_calls_are_not_counted_as_directional(tmp_path):
    t = ForecastTracker(tmp_path / "log.json")
    now = time.time()
    t.record(forecast(direction="flat", prob=50.0, move=0.0), now=now)
    row = t.settle(lambda s: 100.4, now=now + 1000)[0]
    assert row["hit"] is None
    assert t.stats()["graded"] == 0
    assert t.stats()["settled"] == 1


def test_stats_aggregate_hit_rate_models_and_calibration(tmp_path):
    t = ForecastTracker(tmp_path / "log.json", min_gap_sec=0)
    now = time.time()
    # 3 winners, 1 loser across two timeframes
    prices = {}
    for i, (tf, direction, end) in enumerate(
        [("1m", "up", 102.0), ("1m", "up", 101.0), ("5m", "down", 98.0), ("1m", "up", 99.0)]
    ):
        sym = f"S{i}/USDT"
        prices[sym] = end
        t.record(
            forecast(symbol=sym, tf=tf, direction=direction, prob=72.0 if direction == "up" else 30.0),
            now=now,
        )
    t.settle(lambda s: prices[s], now=now + 20_000)
    st = t.stats()
    assert st["settled"] == 4 and st["graded"] == 4
    assert st["hit_rate"] == 75.0
    assert st["edge"] == 25.0
    assert st["brier"] is not None and st["mae_pct"] is not None
    tfs = {r["timeframe"]: r for r in st["by_timeframe"]}
    assert tfs["1m"]["n"] == 3 and tfs["5m"]["n"] == 1
    names = {m["name"] for m in st["by_model"]}
    assert {"Trend", "Mean reversion"} <= names
    assert all(0 <= m["hit_rate"] <= 100 for m in st["by_model"])
    assert st["calibration"] and all(b["n"] > 0 for b in st["calibration"])


def test_pending_rows_expose_countdown(tmp_path):
    t = ForecastTracker(tmp_path / "log.json")
    t.record(forecast())
    st = t.stats()
    assert st["open"] == 1
    assert st["pending"][0]["due_in"] > 0
    assert st["pending"][0]["symbol"] == "BTC/USDT"


def test_tracker_round_trips_through_disk(tmp_path):
    path = tmp_path / "log.json"
    t = ForecastTracker(path, min_gap_sec=0)
    now = time.time()
    t.record(forecast(), now=now)
    t.record(forecast(symbol="ETH/USDT"), now=now)
    t.settle(lambda s: 101.0, now=now + 1000)
    t.save()

    again = ForecastTracker(path)
    assert len(again.settled) == 2
    assert again.stats()["hit_rate"] == 100.0


def test_unpriceable_forecasts_are_dropped_eventually(tmp_path):
    t = ForecastTracker(tmp_path / "log.json")
    now = time.time()
    t.record(forecast(), now=now)
    assert t.settle(lambda s: None, now=now + 1000) == []
    assert len(t.open) == 1  # kept for a while
    assert t.settle(lambda s: None, now=now + 6000) == []
    assert not t.open  # given up on


# --------------------------------------------------------------------------- #
# replay-safe multi-timeframe context
# --------------------------------------------------------------------------- #


def test_mtf_history_builds_higher_frames():
    hist = mtf_history(window(2500))
    assert "5m" in hist.available and "15m" in hist.available
    assert "1m" not in hist.available  # base frame is not resampled onto itself


def test_mtf_history_has_no_look_ahead():
    win = window(2400)
    rows = rows_of(win)
    full = mtf_history(win)
    for cut in (1300, 1811, 2207):
        partial = mtf_history(rows[:cut])
        ts = rows[cut - 1]["ts"]
        assert full.fields_at(ts) == partial.fields_at(ts)


def test_mtf_history_only_exposes_closed_bars():
    win = window(1200)
    hist = mtf_history(win)
    data = hist.frames["15m"]
    # the 22nd bucket is the first that can be scored; nothing is visible before
    # it has fully closed
    first_close = float(data["close_ts"][21])
    assert all(r["tf"] != "15m" for r in hist.rows_at(first_close - 1))
    assert any(r["tf"] == "15m" for r in hist.rows_at(first_close + 1))


def test_mtf_history_fields_match_the_live_flattener():
    hist = mtf_history(window(2000))
    fields = hist.fields_at(time.time())
    for key in ("mtf_score", "mtf_agreement", "mtf_bias", "mtf_verdict", "mtf_timeframes"):
        assert key in fields
    assert any(k.startswith("rsi_") for k in fields)


def test_spec_fields_finds_every_referenced_field():
    spec = {
        "entry": {"op": "all", "rules": [{"left": "trend_1h", "cmp": "==", "right": "up"}]},
        "exit": {"op": "any", "rules": [{"left": "rsi_15m", "cmp": ">", "right": 70}]},
        "short_entry": {"op": "all", "rules": [{"left": "prob_up", "cmp": "<", "right": 40}]},
    }
    found = spec_fields(spec)
    assert {"trend_1h", "rsi_15m", "prob_up"} <= found


def test_backtest_replays_timeframe_rules():
    win = window(2500)
    spec = {
        "id": "t",
        "name": "MTF pullback",
        "side": "long",
        "entry": {
            "op": "all",
            "rules": [
                {"left": "trend_1h", "cmp": "==", "right": "up"},
                {"left": "rsi_15m", "cmp": "<", "right": 45},
            ],
        },
        "exit": {"op": "any", "rules": [{"left": "rsi_15m", "cmp": ">", "right": 70}]},
    }
    res = backtest(win, spec=spec, symbol="BTC/USDT")
    assert res["ok"] is True
    assert res["timeframes"]
    assert any("multi-timeframe" in n for n in res["notes"])
    assert res["metrics"]["trades"] >= 1
    assert res["elapsed_ms"] < 2000


def test_backtest_warns_about_live_only_forecast_fields():
    spec = {
        "id": "t",
        "name": "forecast only",
        "entry": {"op": "all", "rules": [{"left": "prob_up", "cmp": ">", "right": 55}]},
    }
    res = backtest(window(900), spec=spec, symbol="BTC/USDT")
    assert res["ok"] is True
    assert any("forecast fields" in n for n in res["notes"])


def test_backtest_without_timeframe_rules_stays_lean():
    spec = {
        "id": "t",
        "name": "plain",
        "entry": {"op": "all", "rules": [{"left": "rsi", "cmp": "<", "right": 35}]},
    }
    res = backtest(window(900), spec=spec, symbol="BTC/USDT")
    assert res["ok"] is True
    assert res["timeframes"] == []
    assert res["notes"] == []
