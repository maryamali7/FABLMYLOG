import random

import pytest

from app.analytics import analyze, equity_stats
from app.alerts import AlertEngine, normalize_alert, validate_alert
from app.backtest import backtest, compare, portfolio_backtest
from app.custom import (
    TEMPLATES,
    CustomRegistry,
    CustomStrategy,
    normalize_spec,
    template,
    validate_spec,
)
from app.indicators import RollingWindow
from app.models import SignalKind


def _series(n=400, seed=3, drift=0.0006):
    random.seed(seed)
    w = RollingWindow(1000)
    px = 100.0
    for i in range(n):
        px *= 1 + random.gauss(drift, 0.006)
        h = px * (1 + abs(random.gauss(0, 0.002)))
        low = px * (1 - abs(random.gauss(0, 0.002)))
        w.push(i * 60, px, h, low, px, 500 + random.random() * 400)
    return w


# --------------------------------------------------------------------------- #
# builder
# --------------------------------------------------------------------------- #


def test_normalize_spec_fills_defaults():
    spec = normalize_spec({"name": "  My edge  ", "entry": {"op": "all", "rules": []}})
    assert spec["id"].startswith("cs_")
    assert spec["name"] == "My edge"
    assert spec["side"] == "long"
    assert spec["weight"] == 1.0
    assert spec["stop_loss_pct"] is None


def test_validate_spec_requires_conditions():
    assert "add at least one entry condition" in validate_spec({"name": "x", "entry": {"op": "all", "rules": []}})
    good = {"name": "x", "entry": {"op": "all", "rules": [{"left": "rsi", "cmp": "<", "right": 30}]}}
    assert validate_spec(good) == []
    bad_field = {"name": "x", "entry": {"op": "all", "rules": [{"left": "banana", "cmp": "<", "right": 30}]}}
    assert validate_spec(bad_field)


def test_custom_strategy_fires_on_matching_rules():
    win = _series(200, seed=11, drift=-0.002)
    strat = CustomStrategy(
        {
            "name": "always long",
            "confidence": 0.8,
            "cooldown_sec": 0,
            "entry": {"op": "all", "rules": [{"left": "rsi", "cmp": "<", "right": 99}]},
        }
    )
    sig = strat.evaluate("TEST/USDT", win, float(win.closes[-1]))
    assert sig is not None
    assert sig.kind == SignalKind.BUY
    assert sig.extras["custom"] is True


def test_custom_strategy_respects_cooldown_and_symbols():
    win = _series(200, seed=5)
    strat = CustomStrategy(
        {
            "name": "cool",
            "cooldown_sec": 9999,
            "entry": {"op": "all", "rules": [{"left": "close", "cmp": ">", "right": 0}]},
        }
    )
    assert strat.evaluate("A/USDT", win, 100.0) is not None
    assert strat.evaluate("A/USDT", win, 100.0) is None  # cooled down

    scoped = CustomStrategy(
        {
            "name": "scoped",
            "symbols": ["ONLY/USDT"],
            "cooldown_sec": 0,
            "entry": {"op": "all", "rules": [{"left": "close", "cmp": ">", "right": 0}]},
        }
    )
    assert scoped.evaluate("OTHER/USDT", win, 100.0) is None
    assert scoped.evaluate("ONLY/USDT", win, 100.0) is not None


def test_exit_rules_emit_sell():
    win = _series(200, seed=9)
    strat = CustomStrategy(
        {
            "name": "exit now",
            "cooldown_sec": 0,
            "entry": {"op": "all", "rules": [{"left": "rsi", "cmp": "<", "right": 0}]},
            "exit": {"op": "all", "rules": [{"left": "close", "cmp": ">", "right": 0}]},
        }
    )
    sig = strat.evaluate("TEST/USDT", win, float(win.closes[-1]))
    assert sig is not None and sig.kind == SignalKind.SELL


def test_every_template_is_valid_and_loadable():
    for t in TEMPLATES:
        spec = template(t["template_id"])
        assert spec is not None
        assert validate_spec(spec) == []
        assert CustomStrategy(spec).title == t["name"]
    assert template("does-not-exist") is None


def test_registry_crud_roundtrip(tmp_path):
    reg = CustomRegistry(tmp_path / "custom.json")
    strat, errors = reg.upsert(
        {"name": "roundtrip", "entry": {"op": "all", "rules": [{"left": "rsi", "cmp": "<", "right": 30}]}}
    )
    assert errors == [] and strat is not None
    sid = strat.spec["id"]
    assert reg.toggle(sid) is False
    assert reg.toggle(sid, True) is True
    copy = reg.duplicate(sid)
    assert copy and copy.spec["id"] != sid
    reloaded = CustomRegistry(tmp_path / "custom.json")
    assert len(reloaded.list()) == 2
    assert reloaded.delete(sid) is True
    assert reloaded.delete("missing") is False


def test_registry_rejects_bad_spec(tmp_path):
    reg = CustomRegistry(tmp_path / "custom.json")
    strat, errors = reg.upsert({"name": "", "entry": None})
    assert strat is None and errors


# --------------------------------------------------------------------------- #
# backtest
# --------------------------------------------------------------------------- #


def test_backtest_runs_and_reports_metrics():
    win = _series(500, seed=21)
    spec = template("macd_momentum")
    res = backtest(win, spec=spec, symbol="TEST/USDT")
    assert res["ok"] is True
    m = res["metrics"]
    for key in ("trades", "win_rate", "profit_factor", "max_drawdown_pct", "sharpe", "grade"):
        assert key in m
    assert len(res["equity_curve"]) > 5
    assert res["metrics"]["trades"] == len([t for t in res["trades"]]) or res["metrics"]["trades"] >= len(res["trades"])


def test_backtest_needs_enough_candles():
    win = _series(20)
    assert backtest(win, spec=template("macd_momentum"))["ok"] is False
    assert backtest(_series(200), builtin="does_not_exist")["ok"] is False
    assert backtest(_series(200))["ok"] is False


def test_backtest_respects_stops():
    win = _series(400, seed=33)
    spec = {
        "name": "tight",
        "cooldown_sec": 0,
        "stop_loss_pct": 0.002,
        "take_profit_pct": 0.002,
        "entry": {"op": "all", "rules": [{"left": "close", "cmp": ">", "right": 0}]},
    }
    res = backtest(win, spec=spec, symbol="TEST/USDT")
    assert res["ok"]
    assert res["metrics"]["trades"] > 3
    assert all(t["reason"] in ("stop", "target", "trail", "time stop", "signal exit") for t in res["trades"])


def test_backtest_builtin_and_compare():
    win = _series(300, seed=4)
    res = backtest(win, builtin="rsi_reversion", symbol="TEST/USDT")
    assert res["ok"] is True
    ranked = compare(win, ["rsi_reversion", "macd_trend"], "TEST/USDT")
    assert len(ranked) == 2
    assert ranked[0]["return_pct"] >= ranked[1]["return_pct"]


def test_portfolio_backtest_aggregates():
    series = {"A/USDT": _series(300, seed=1), "B/USDT": _series(300, seed=2)}
    out = portfolio_backtest(series, spec=template("macd_momentum"))
    assert out["ok"] is True
    assert out["totals"]["symbols"] == 2
    assert "best" in out["totals"]


# --------------------------------------------------------------------------- #
# alerts
# --------------------------------------------------------------------------- #


def test_alert_validation():
    assert validate_alert({"name": "", "rule": None})
    good = {"name": "a", "rule": {"op": "all", "rules": [{"left": "alpha", "cmp": ">", "right": 70}]}}
    assert validate_alert(good) == []
    bad_hook = {**good, "webhook": "ftp://x"}
    assert validate_alert(bad_hook)


def test_alert_engine_fires_with_cooldown(tmp_path):
    eng = AlertEngine(tmp_path / "alerts.json")
    rule, errors = eng.upsert(
        {
            "name": "hot",
            "cooldown_sec": 600,
            "message": "{symbol} is hot",
            "rule": {"op": "all", "rules": [{"left": "alpha", "cmp": ">", "right": 70}]},
        }
    )
    assert errors == []
    rows = [{"symbol": "AAA/USDT", "alpha": 88, "last": 5}, {"symbol": "BBB/USDT", "alpha": 10, "last": 2}]
    fired = eng.evaluate(rows)
    assert len(fired) == 1
    assert fired[0]["symbol"] == "AAA/USDT"
    assert fired[0]["text"] == "AAA/USDT is hot"
    assert eng.evaluate(rows) == []  # cooldown
    assert eng.rules[rule["id"]]["hits"] == 1
    assert eng.recent(5)


def test_alert_scope_and_toggle(tmp_path):
    eng = AlertEngine(tmp_path / "alerts.json")
    rule, _ = eng.upsert(
        {
            "name": "scoped",
            "symbols": ["ZZZ/USDT"],
            "rule": {"op": "all", "rules": [{"left": "alpha", "cmp": ">", "right": 1}]},
        }
    )
    assert eng.evaluate([{"symbol": "AAA/USDT", "alpha": 90}]) == []
    assert eng.toggle(rule["id"]) is False
    assert eng.evaluate([{"symbol": "ZZZ/USDT", "alpha": 90}]) == []
    assert eng.toggle(rule["id"]) is True
    assert len(eng.evaluate([{"symbol": "ZZZ/USDT", "alpha": 90}])) == 1
    assert eng.delete(rule["id"]) is True


def test_normalize_alert_clamps_cooldown():
    spec = normalize_alert({"name": "x", "cooldown_sec": 1, "severity": "nope"})
    assert spec["cooldown_sec"] == 30
    assert spec["severity"] == "info"


# --------------------------------------------------------------------------- #
# analytics
# --------------------------------------------------------------------------- #


def test_analyze_builds_tables():
    fills = [
        {"symbol": "A/USDT", "side": "sell", "pnl": 12.0, "strategy": "s1", "ts": 1700000000, "fee": 0.4, "qty": 1, "price": 10, "reason": "target"},
        {"symbol": "A/USDT", "side": "sell", "pnl": -5.0, "strategy": "s1", "ts": 1700003600, "fee": 0.4, "qty": 1, "price": 10, "reason": "stop"},
        {"symbol": "B/USDT", "side": "sell", "pnl": 7.0, "strategy": "s2", "ts": 1700007200, "fee": 0.2, "qty": 1, "price": 10, "reason": "target"},
    ]
    equity = [{"ts": 1700000000 + i * 60, "equity": 10000 + i * 3} for i in range(30)]
    out = analyze(fills, equity)
    assert out["overall"]["trades"] == 3
    assert out["overall"]["win_rate"] == pytest.approx(66.67, abs=0.1)
    assert out["by_strategy"][0]["strategy"] in ("s1", "s2")
    assert len(out["by_symbol"]) == 2
    assert out["equity"]["points"] == 30
    assert out["pnl_histogram"]


def test_equity_stats_handles_short_series():
    assert equity_stats([])["points"] == 0
    stats = equity_stats([{"ts": i, "equity": 100 - i} for i in range(40)])
    assert stats["max_drawdown_pct"] > 0
    assert stats["return_pct"] < 0


def test_string_literal_conditions_validate():
    from app.rules import validate_rule

    assert validate_rule({"op": "all", "rules": [{"left": "grade", "cmp": "==", "right": "A"}]}) == []
    assert validate_rule({"op": "all", "rules": [{"left": "supertrend", "cmp": "==", "right": "bull"}]}) == []
    spec = {
        "name": "grade filter",
        "entry": {"op": "all", "rules": [{"left": "close", "cmp": ">", "right": "ema21"}]},
    }
    assert validate_spec(spec) == []


def test_backtest_warmup_adapts_to_short_history():
    win = _series(120, seed=17)
    res = backtest(
        win,
        spec={
            "name": "short",
            "cooldown_sec": 0,
            "take_profit_pct": 0.005,
            "entry": {"op": "all", "rules": [{"left": "close", "cmp": ">", "right": 0}]},
        },
        symbol="TEST/USDT",
    )
    assert res["ok"] is True
    assert res["metrics"]["trades"] >= 1


def test_ensemble_carries_custom_risk_overrides():
    from app.models import Signal
    from app.strategies import ensemble

    now = 1.0
    sigs = [
        Signal("cs_1", "BTC/USDT", SignalKind.BUY, 0.9, 100, "custom", now,
               extras={"stop_loss_pct": 0.01, "take_profit_pct": 0.05, "spec_id": "cs_1"}),
        Signal("macd_trend", "BTC/USDT", SignalKind.BUY, 0.6, 100, "builtin", now),
    ]
    out = ensemble(sigs, 0.5)
    assert out is not None
    assert out.extras["stop_loss_pct"] == 0.01
    assert out.extras["take_profit_pct"] == 0.05
    assert out.extras["spec_id"] == "cs_1"
    assert "cs_1" in out.extras["contributors"]
