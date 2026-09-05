"""Order management, matching rules, portfolio risk and the trade journal."""

from __future__ import annotations

import time

import pytest

from app.orders import OMS, Order, OrderError
from app.portfolio import (
    Journal,
    correlations,
    exposure,
    open_risk,
    r_distribution,
    summary,
    value_at_risk,
)


def book(tmp_path=None) -> OMS:
    return OMS(tmp_path / "orders.json" if tmp_path else None)


def mk(**kw) -> Order:
    base = {"symbol": "BTC/USDT", "side": "buy", "type": "market", "qty": 1.0}
    base.update(kw)
    return Order(**base)


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #


def test_market_order_rests_and_fills_at_the_offer():
    oms = book()
    oms.place(mk(), last=100, bid=99.9, ask=100.1)
    intents = oms.match("BTC/USDT", 100, 99.9, 100.1)
    assert len(intents) == 1
    assert intents[0]["price"] == 100.1 and intents[0]["qty"] == 1.0
    oms.confirm(intents[0]["order"], 1.0, 100.1)
    assert oms.working() == []
    assert oms.history[0]["event"] == "filled"


def test_quote_sizing_converts_to_base():
    oms = book()
    order = oms.place(mk(qty=0, quote_qty=500), last=100, ask=100)
    assert order.qty == pytest.approx(5.0)


@pytest.mark.parametrize(
    "kw,msg",
    [
        ({"side": "long"}, "side must be"),
        ({"type": "iceberg"}, "unknown order type"),
        ({"tif": "gtd"}, "unknown time in force"),
        ({"qty": 0}, "needs a quantity"),
        ({"type": "limit", "price": 0}, "need a limit price"),
        ({"type": "stop", "stop_price": 0}, "need a stop price"),
        ({"type": "trailing_stop", "side": "sell"}, "trail percentage"),
        ({"type": "trailing_stop", "side": "buy", "trail_pct": 0.01}, "sell side only"),
    ],
)
def test_bad_orders_are_rejected_with_a_reason(kw, msg):
    oms = book()
    with pytest.raises(OrderError) as err:
        oms.place(mk(**kw), last=100, bid=99, ask=101)
    assert msg in str(err.value)


def test_post_only_refuses_to_cross():
    oms = book()
    with pytest.raises(OrderError):
        oms.place(mk(type="limit", price=101, post_only=True), last=100, bid=99.9, ask=100.1)
    resting = oms.place(mk(type="limit", price=95, post_only=True), last=100, bid=99.9, ask=100.1)
    assert resting.status == "working"


# --------------------------------------------------------------------------- #
# matching rules
# --------------------------------------------------------------------------- #


def test_limit_buy_waits_for_its_price():
    oms = book()
    oms.place(mk(type="limit", price=95), last=100, bid=99.9, ask=100.1)
    assert oms.match("BTC/USDT", 100, 99.9, 100.1) == []
    intents = oms.match("BTC/USDT", 94.5, 94.4, 94.6)
    assert intents and intents[0]["price"] == pytest.approx(94.6)
    assert intents[0]["price"] <= 95, "never fill a limit worse than its price"


def test_limit_sell_waits_for_its_price():
    oms = book()
    oms.place(mk(side="sell", type="limit", price=110), last=100, bid=99.9, ask=100.1)
    assert oms.match("BTC/USDT", 105, 105, 105.1, position_qty=5) == []
    intents = oms.match("BTC/USDT", 111, 111, 111.2, position_qty=5)
    assert intents and intents[0]["price"] >= 110


def test_stop_arms_then_fires():
    oms = book()
    order = oms.place(mk(side="sell", type="stop", stop_price=90, reduce_only=True), last=100)
    assert oms.match("BTC/USDT", 95, 95, 95.1, position_qty=1) == []
    assert order.status == "working"
    intents = oms.match("BTC/USDT", 89, 88.9, 89.1, position_qty=1)
    assert order.status == "triggered"
    assert intents and intents[0]["price"] == pytest.approx(88.9)


def test_stop_limit_becomes_a_limit_after_trigger():
    oms = book()
    order = oms.place(mk(side="sell", type="stop_limit", stop_price=95, price=94), last=100)
    # trigger, but the market is through the limit — no fill
    assert oms.match("BTC/USDT", 94.5, 93.0, 93.1, position_qty=1) == []
    assert order.status == "triggered"
    intents = oms.match("BTC/USDT", 94.5, 94.5, 94.6, position_qty=1)
    assert intents and intents[0]["price"] >= 94


def test_trailing_stop_ratchets_up_and_never_down():
    oms = book()
    order = oms.place(mk(side="sell", type="trailing_stop", trail_pct=0.05, reduce_only=True), last=100)
    oms.match("BTC/USDT", 100, 100, 100, position_qty=1)
    assert order.stop_price == pytest.approx(95.0)
    oms.match("BTC/USDT", 120, 120, 120, position_qty=1)
    assert order.stop_price == pytest.approx(114.0), "trail follows the high"
    oms.match("BTC/USDT", 118, 118, 118, position_qty=1)
    assert order.stop_price == pytest.approx(114.0), "and never slides back down"
    intents = oms.match("BTC/USDT", 113, 113, 113.1, position_qty=1)
    assert intents and order.status == "triggered"


def test_ioc_cancels_when_it_cannot_fill_now():
    oms = book()
    oms.place(mk(type="limit", price=90, tif="ioc"), last=100, bid=99.9, ask=100.1)
    assert oms.match("BTC/USDT", 100, 99.9, 100.1) == []
    assert oms.working() == []
    assert any(h["event"] == "cancelled" for h in oms.history)


def test_day_orders_expire():
    oms = book()
    order = oms.place(mk(type="limit", price=1, tif="day"), last=100)
    assert order.expires_ts > time.time()
    order.expires_ts = time.time() - 1
    assert oms.match("BTC/USDT", 100, 100, 100) == []
    assert order.status == "expired" and oms.working() == []


def test_reduce_only_is_dropped_without_a_position():
    oms = book()
    oms.place(mk(side="sell", type="limit", price=1, reduce_only=True), last=100)
    oms.match("BTC/USDT", 100, 100, 100, position_qty=0)
    assert oms.working() == []
    oms2 = book()
    o = oms2.place(mk(side="sell", type="limit", price=1, qty=5, reduce_only=True), last=100)
    intents = oms2.match("BTC/USDT", 100, 100, 100, position_qty=2)
    assert intents[0]["qty"] == 2, "never sell more than the position"
    assert o.status == "working"


def test_partial_fills_track_average_price():
    oms = book()
    order = oms.place(mk(qty=10), last=100)
    oms.confirm(order, 4, 100.0)
    assert order.status == "working" and order.remaining == 6
    oms.confirm(order, 6, 101.0)
    assert order.status == "filled"
    assert order.avg_fill == pytest.approx((4 * 100 + 6 * 101) / 10)


# --------------------------------------------------------------------------- #
# brackets / OCO
# --------------------------------------------------------------------------- #


def test_bracket_is_one_cancels_the_other():
    oms = book()
    stop, target = oms.attach_bracket("BTC/USDT", qty=2, stop_price=90, take_price=120)
    assert stop.type == "stop" and target.type == "limit"
    assert stop.oco_group == target.oco_group and stop.reduce_only
    assert len(oms.working()) == 2
    oms.confirm(target, 2, 120)
    assert target.status == "filled"
    assert stop.status == "cancelled" and stop.reason == "OCO sibling filled"
    assert oms.working() == []


def test_bracket_can_include_a_trail():
    oms = book()
    orders = oms.attach_bracket("BTC/USDT", qty=1, stop_price=90, take_price=120, trail_pct=0.03)
    assert {o.type for o in orders} == {"stop", "limit", "trailing_stop"}
    assert len({o.oco_group for o in orders}) == 1


def test_cancel_all_by_symbol_and_side():
    oms = book()
    oms.place(mk(type="limit", price=90), last=100)
    oms.place(mk(symbol="ETH/USDT", type="limit", price=90), last=100)
    oms.place(mk(side="sell", type="limit", price=200, reduce_only=True), last=100)
    assert oms.cancel_all(symbol="BTC/USDT", side="buy") == 1
    assert {o["symbol"] for o in oms.working()} == {"ETH/USDT", "BTC/USDT"}
    assert oms.cancel_all() == 2 and oms.working() == []


def test_modify_price_and_quantity():
    oms = book()
    order = oms.place(mk(type="limit", price=90, qty=1), last=100)
    oms.modify(order.id, price=95, qty=3)
    assert order.price == 95 and order.qty == 3
    oms.confirm(order, 3, 95)
    with pytest.raises(OrderError):
        oms.modify(order.id, price=99)


def test_orders_survive_a_restart(tmp_path):
    oms = book(tmp_path)
    oms.place(mk(type="limit", price=90), last=100)
    again = OMS(tmp_path / "orders.json")
    assert len(again.working()) == 1
    assert again.working()[0]["price"] == 90
    assert again.history, "history is persisted too"


def test_snapshot_shape():
    oms = book()
    oms.place(mk(type="limit", price=90), last=100)
    snap = oms.snapshot()
    assert snap["count"] == 1 and snap["by_symbol"] == {"BTC/USDT": 1}
    assert "market" in snap["types"] and "gtc" in snap["tifs"]


# --------------------------------------------------------------------------- #
# portfolio risk
# --------------------------------------------------------------------------- #


POSITIONS = [
    {"symbol": "BTC/USDT", "qty": 0.05, "entry": 60000, "price": 62000, "stop": 58000, "unrealized": 100},
    {"symbol": "ETH/USDT", "qty": 1.0, "entry": 3000, "price": 3100, "stop": 2900, "unrealized": 100},
    {"symbol": "SOL/USDT", "qty": 5.0, "entry": 150, "price": 152, "stop": 0, "unrealized": 10},
]


def test_exposure_and_concentration():
    exp = exposure(POSITIONS, equity=10_000, cash=5_000)
    assert exp["positions"] == 3
    assert exp["rows"][0]["symbol"] == "BTC/USDT"  # biggest first
    assert exp["gross"] == pytest.approx(0.05 * 62000 + 3100 + 5 * 152)
    assert exp["cash_pct"] == 50.0
    assert 0 < exp["concentration"] <= 1
    assert exp["concentration_label"] in ("diversified", "concentrated", "single-name risk")
    # fields the desk UI reads
    assert exp["net"] == exp["gross"], "long-only book: net equals gross"
    assert sum(r["weight"] for r in exp["rows"]) == pytest.approx(1.0, abs=0.01)
    assert exp["rows"][0]["notional"] == exp["rows"][0]["value"]
    assert exp["gross_pct"] == pytest.approx(exp["gross"] / 10_000 * 100, abs=0.01)


def test_exposure_nets_off_a_short():
    rows = [
        {"symbol": "BTC/USDT", "qty": 0.1, "entry": 60000, "price": 60000, "side": "buy"},
        {"symbol": "ETH/USDT", "qty": 1.0, "entry": 3000, "price": 3000, "side": "sell"},
    ]
    exp = exposure(rows, equity=10_000, cash=1_000)
    assert exp["gross"] == pytest.approx(9000)
    assert exp["net"] == pytest.approx(3000)
    assert [r["side"] for r in exp["rows"]] == ["long", "short"]


def test_open_risk_flags_unprotected_positions():
    risk = open_risk(POSITIONS, equity=10_000)
    assert risk["unprotected"] == ["SOL/USDT"]
    btc = next(r for r in risk["rows"] if r["symbol"] == "BTC/USDT")
    assert btc["risk"] == pytest.approx((62000 - 58000) * 0.05)
    assert risk["total"] == pytest.approx(200 + 200)
    assert risk["pct_equity"] == pytest.approx(4.0)
    # BTC is 2000 above a 60000 entry with 2000 of initial risk => +1R open
    assert btc["r_open"] == pytest.approx(1.0)
    assert next(r for r in risk["rows"] if r["symbol"] == "SOL/USDT")["r_open"] is None


def test_correlations_detect_a_single_bet():
    same = [100 * (1.01**i) for i in range(60)]
    series = {"BTC/USDT": same, "ETH/USDT": same, "SOL/USDT": list(reversed(same))}
    corr = correlations(series)
    assert corr["symbols"][:2] == ["BTC/USDT", "ETH/USDT"]
    assert corr["matrix"][0][1] == pytest.approx(1.0, abs=0.01)
    assert corr["most_correlated"][0]["corr"] != 0
    assert corr["diversification"] in ("positions move together", "moderately linked", "well spread")


def test_var_needs_history_then_reports():
    assert value_at_risk([{"equity": 100}, {"equity": 101}], 100)["ok"] is False
    curve = [{"equity": 10_000 * (1 + (i % 7 - 3) / 100)} for i in range(80)]
    var = value_at_risk(curve, 10_000)
    assert var["ok"] and var["samples"] >= 20
    assert var["var95_pct"] < 0 and var["var95_value"] > 0
    assert var["expected_shortfall_pct"] <= var["var95_pct"]
    assert var["worst_pct"] <= var["var95_pct"]
    assert var["vol_pct"] == var["volatility_pct"]


def test_r_distribution_shows_where_the_money_comes_from():
    fills = [{"r": r} for r in (2.5, 1.2, -1.0, -1.0, 0.5, 4.0, -0.8, -1.0)]
    dist = r_distribution(fills)
    assert dist["ok"] and dist["trades"] == 8
    assert dist["best_r"] == 4.0 and dist["worst_r"] == -1.0
    assert dist["expectancy_r"] == pytest.approx(sum(f["r"] for f in fills) / 8, abs=1e-6)
    assert sum(b["count"] for b in dist["buckets"]) == 8
    assert 0 < dist["top_decile_share"] <= 1
    assert r_distribution([])["ok"] is False


def test_summary_warns_about_the_dangerous_book():
    same = [100 * (1.01**i) for i in range(60)]
    out = summary(
        positions=POSITIONS,
        equity=10_000,
        cash=5_000,
        equity_curve=[{"equity": 10_000 + i} for i in range(40)],
        series={"BTC/USDT": same, "ETH/USDT": same},
        fills=[{"r": 1.0, "pnl": 10, "id": "a"}],
    )
    joined = " ".join(out["warnings"])
    assert "no stop on SOL/USDT" in joined
    assert "exposure" in out and "var" in out and "r_distribution" in out


# --------------------------------------------------------------------------- #
# journal
# --------------------------------------------------------------------------- #


def test_journal_tags_notes_and_aggregation(tmp_path):
    j = Journal(tmp_path / "journal.json")
    j.annotate("f1", note="chased the breakout", tags=["Breakout", " fomo "], rating=2)
    j.annotate("f2", tags=["breakout"], rating=5)
    assert j.get("f1")["tags"] == ["breakout", "fomo"], "tags are normalised"
    assert j.get("f1")["rating"] == 2
    assert j.tags() == ["breakout", "fomo"]

    rows = j.by_tag([{"id": "f1", "pnl": -50.0}, {"id": "f2", "pnl": 150.0}])
    breakout = next(r for r in rows if r["tag"] == "breakout")
    assert breakout["trades"] == 2 and breakout["net"] == 100.0
    assert breakout["win_rate"] == 0.5
    fomo = next(r for r in rows if r["tag"] == "fomo")
    assert fomo["net"] == -50.0

    # survives a reload
    again = Journal(tmp_path / "journal.json")
    assert again.get("f2")["rating"] == 5
    assert again.get("missing") == {"tags": [], "note": "", "rating": 0}


def test_journal_rating_is_clamped(tmp_path):
    j = Journal(tmp_path / "journal.json")
    assert j.annotate("x", rating=99)["rating"] == 5
    assert j.annotate("y", rating=-4)["rating"] == 0


# --------------------------------------------------------------------------- #
# entry + bracket placed together
# --------------------------------------------------------------------------- #


def test_bracket_children_wait_for_their_entry():
    """The protective pair is born before the position exists — it must survive."""
    oms = book()
    entry = oms.place(mk(type="limit", price=95, qty=2), last=100, bid=99.9, ask=100.1)
    stop, target = oms.attach_bracket("BTC/USDT", qty=2, stop_price=90, take_price=120, parent_id=entry.id)

    # market is nowhere near the entry: nothing fills, nothing is culled
    assert oms.match("BTC/USDT", 100, 99.9, 100.1, position_qty=0) == []
    assert stop.status == "working" and target.status == "working"

    # entry fills
    intents = oms.match("BTC/USDT", 94.5, 94.4, 94.6, position_qty=0)
    assert intents and intents[0]["order"] is entry
    oms.confirm(entry, 2, 94.6)

    # now the children are live and the stop can fire
    fired = oms.match("BTC/USDT", 89, 88.9, 89.1, position_qty=2)
    assert [i["order"] for i in fired] == [stop]


def test_cancelling_an_unfilled_entry_takes_its_bracket_with_it():
    oms = book()
    entry = oms.place(mk(type="limit", price=95), last=100, bid=99.9, ask=100.1)
    stop, target = oms.attach_bracket("BTC/USDT", qty=1, stop_price=90, take_price=120, parent_id=entry.id)
    oms.cancel(entry.id, "changed my mind")
    assert stop.status == "cancelled" and stop.reason == "entry cancelled"
    assert target.status == "cancelled"
    assert oms.working() == []


def test_a_filled_entry_leaves_its_bracket_alone():
    oms = book()
    entry = oms.place(mk(qty=1), last=100, bid=99.9, ask=100.1)
    stop, _ = oms.attach_bracket("BTC/USDT", qty=1, stop_price=90, take_price=120, parent_id=entry.id)
    oms.confirm(entry, 1, 100.1)
    assert stop.status == "working", "the position still needs protecting"
    assert len(oms.working()) == 2
