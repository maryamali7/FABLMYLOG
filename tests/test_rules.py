import numpy as np

from app.indicators import RollingWindow
from app.rules import (
    ALL_FIELDS,
    Feat,
    ExpressionError,
    compute_frame,
    context_at,
    context_from_row,
    count_conditions,
    eval_expression,
    evaluate_rule,
    field_catalog,
    validate_rule,
)


def _win(n=200, start=100.0, drift=1.002):
    w = RollingWindow(400)
    px = start
    for i in range(n):
        px *= drift
        w.push(i * 60, px / drift, px * 1.004, px * 0.996, px, 100 + (i % 7) * 10)
    return w, px


def test_frame_has_every_catalog_field():
    w, _ = _win()
    frame = compute_frame(w)
    missing = [k for k in ALL_FIELDS if k not in frame and ALL_FIELDS[k]["group"] != "Screener"]
    assert missing == []
    assert len(frame["close"]) == 200


def test_frame_arrays_are_finite():
    w, _ = _win(120, 50, 0.997)
    frame = compute_frame(w)
    for key, arr in frame.items():
        if key.startswith("_"):
            continue
        assert np.isfinite(np.asarray(arr, dtype=float)).all(), key


def test_context_carries_previous_bar():
    w, _ = _win(80)
    ctx = context_at(compute_frame(w))
    assert isinstance(ctx["close"], Feat)
    assert ctx["close"].prev > 0
    assert ctx["close"] > ctx["close"].prev  # uptrend


def test_expression_whitelist_blocks_attacks():
    ctx = {"rsi": Feat(30, 40)}
    assert eval_expression("rsi < 35", ctx) is True
    for bad in ("__import__('os')", "open('x')", "rsi.__class__", "[i for i in range(3)]"):
        try:
            eval_expression(bad, ctx)
            raise AssertionError(f"should have rejected {bad}")
        except ExpressionError:
            pass


def test_cross_helpers():
    ctx = {"ema9": Feat(10, 8), "ema21": Feat(9, 9)}
    ok, _ = evaluate_rule({"left": "ema9", "cmp": "cross_above", "right": "ema21"}, ctx)
    assert ok
    ok, _ = evaluate_rule({"left": "ema9", "cmp": "cross_below", "right": "ema21"}, ctx)
    assert not ok


def test_group_operators():
    ctx = {"rsi": Feat(25, 28), "adx": Feat(30, 29)}
    rule = {
        "op": "all",
        "rules": [
            {"left": "rsi", "cmp": "<", "right": 30},
            {"op": "any", "rules": [{"left": "adx", "cmp": ">", "right": 50}, {"left": "adx", "cmp": ">", "right": 20}]},
        ],
    }
    ok, trace = evaluate_rule(rule, ctx)
    assert ok and len(trace) == 3
    none_rule = {"op": "none", "rules": [{"left": "rsi", "cmp": ">", "right": 90}]}
    assert evaluate_rule(none_rule, ctx)[0]


def test_between_and_string_fields():
    ctx = context_from_row({"rsi": 45, "bias": "long", "squeeze": True})
    assert evaluate_rule({"left": "rsi", "cmp": "between", "right": [40, 50]}, ctx)[0]
    assert not evaluate_rule({"left": "rsi", "cmp": "outside", "right": [40, 50]}, ctx)[0]
    assert evaluate_rule({"left": "bias", "cmp": "==", "right": "long"}, ctx)[0]
    assert evaluate_rule({"left": "squeeze", "cmp": "is_true"}, ctx)[0]


def test_validate_rule_reports_problems():
    errs = validate_rule({"op": "all", "rules": [{"left": "nope", "cmp": "<", "right": 3}]})
    assert errs
    assert validate_rule({"op": "all", "rules": [{"left": "rsi", "cmp": "<", "right": 30}]}) == []
    assert validate_rule({"op": "all", "rules": [{"left": "rsi", "cmp": "wat", "right": 30}]})


def test_count_conditions_and_catalog():
    rule = {"op": "all", "rules": [{"left": "rsi", "cmp": "<", "right": 3}, {"op": "any", "rules": [{"expr": "adx > 2"}]}]}
    assert count_conditions(rule) == 2
    groups = field_catalog()
    assert any(g["group"] == "Momentum" for g in groups)
