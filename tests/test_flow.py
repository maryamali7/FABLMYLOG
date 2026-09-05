"""Consolidated tape, volume dots, cumulative delta and the order-flow read."""

from __future__ import annotations

import pytest

from app.flow import build_dots, read_flow, summarise, venue_split
from app.tape import TapeBook, estimate_bars


def feed(tape: TapeBook, sym: str, rows: list[tuple[float, float, float, str, str]]) -> None:
    """rows: (ts, price, qty, side, venue)"""
    for ts, price, qty, side, venue in rows:
        tape.record(sym, price, qty, side, venue, ts)


# --------------------------------------------------------------------------- #
# recording
# --------------------------------------------------------------------------- #


def test_prints_bucket_into_cells_and_keep_the_side():
    t = TapeBook(cell_seconds=5)
    feed(t, "BTC/USDT", [
        (1000.0, 100.0, 2.0, "buy", "binance"),
        (1001.0, 101.0, 1.0, "sell", "okx"),
        (1004.9, 102.0, 3.0, "buy", "binance"),
        (1005.0, 103.0, 1.0, "sell", "bybit"),   # next cell
    ])
    cells = list(t.cells["BTC/USDT"])
    assert len(cells) == 2, "5s cells"
    first = cells[0]
    assert first.buy_vol == 5.0 and first.sell_vol == 1.0
    assert first.delta == 4.0 and first.volume == 6.0
    assert first.buy_trades == 2 and first.sell_trades == 1
    assert set(first.venues) == {"binance", "okx"}
    assert cells[1].venues["bybit"] == [0.0, 1.0]


def test_side_labels_are_normalised():
    t = TapeBook()
    feed(t, "X/USDT", [(0.0, 10.0, 1.0, "BUY", "a"), (0.0, 10.0, 1.0, "Sell", "a"),
                       (0.0, 10.0, 1.0, "b", "a")])
    cell = list(t.cells["X/USDT"])[0]
    assert cell.buy_vol == 2.0 and cell.sell_vol == 1.0


def test_junk_is_ignored():
    t = TapeBook()
    t.record("", 10, 1, "buy", "a")
    t.record("X/USDT", 0, 1, "buy", "a")
    t.record("X/USDT", 10, 0, "buy", "a")
    t.record("X/USDT", 10, -5, "buy", "a")
    assert not t.cells


def test_memory_is_bounded():
    t = TapeBook(cell_seconds=5, max_cells=10)
    for i in range(200):
        t.record("X/USDT", 100.0, 1.0, "buy", "a", ts=i * 5.0)
    assert len(t.cells["X/USDT"]) == 10, "old cells roll off"
    assert t.counts["X/USDT"] == 200, "but the count is not lost"


def test_a_late_print_lands_in_its_own_cell():
    t = TapeBook(cell_seconds=5)
    feed(t, "X/USDT", [(1000.0, 100.0, 1.0, "buy", "a"), (1010.0, 100.0, 1.0, "buy", "a")])
    t.record("X/USDT", 100.0, 4.0, "sell", "a", ts=1001.0)   # arrives out of order
    cells = list(t.cells["X/USDT"])
    assert cells[0].sell_vol == 4.0
    assert cells[-1].sell_vol == 0.0


def test_outsized_prints_are_remembered():
    t = TapeBook()
    for i in range(60):
        t.record("X/USDT", 100.0, 1.0, "buy", "binance", ts=1000.0 + i)
    t.record("X/USDT", 100.0, 40.0, "sell", "okx", ts=1100.0)
    big = t.big_prints("X/USDT")
    assert big and big[0]["side"] == "sell" and big[0]["venue"] == "okx"
    assert big[0]["ratio"] > 4
    assert big[0]["notional"] == pytest.approx(4000.0)


def test_coverage_reports_what_was_seen():
    t = TapeBook(cell_seconds=5)
    feed(t, "X/USDT", [(0.0, 10.0, 1.0, "buy", "binance"), (60.0, 10.0, 1.0, "sell", "okx")])
    cov = t.coverage("X/USDT")
    assert cov["venues"] == ["binance", "okx"]
    assert cov["prints"] == 2 and cov["seconds"] == 60.0
    assert t.stats()["symbols"] == 1


# --------------------------------------------------------------------------- #
# resampling
# --------------------------------------------------------------------------- #


def test_cells_resample_into_bars():
    t = TapeBook(cell_seconds=5)
    rows = []
    for i in range(24):          # 24 cells = 2 minutes
        rows.append((i * 5.0, 100.0 + i, 1.0, "buy" if i % 2 else "sell", "binance"))
    feed(t, "X/USDT", rows)
    bars = t.bars("X/USDT", 60, 10)
    assert len(bars) == 2, "two 1m bars"
    first = bars[0]
    assert first["volume"] == 12.0
    assert first["high"] >= first["low"]
    assert first["open"] and first["close"]
    assert first["trades"] == 12
    assert first["estimated"] is False
    assert first["vwap"] > 0
    assert bars[1]["ts"] - bars[0]["ts"] == 60


def test_bars_carry_the_venue_split():
    t = TapeBook(cell_seconds=5)
    feed(t, "X/USDT", [
        (0.0, 100.0, 3.0, "buy", "binance"),
        (5.0, 100.0, 1.0, "sell", "okx"),
        (10.0, 100.0, 2.0, "buy", "okx"),
    ])
    bar = t.bars("X/USDT", 60, 5)[0]
    assert bar["venues"]["binance"] == {"buy": 3.0, "sell": 0.0}
    assert bar["venues"]["okx"] == {"buy": 2.0, "sell": 1.0}
    rows = venue_split([bar])
    assert rows[0]["venue"] == "binance" or rows[0]["volume"] >= rows[1]["volume"]
    okx = next(r for r in rows if r["venue"] == "okx")
    assert okx["delta"] == 1.0 and okx["share"] < 1


def test_bars_of_an_unseen_symbol_are_empty():
    assert TapeBook().bars("NOPE/USDT", 60) == []


# --------------------------------------------------------------------------- #
# the candle-shape fallback
# --------------------------------------------------------------------------- #


def test_close_position_estimates_the_split():
    bars = estimate_bars([
        {"ts": 0, "open": 10, "high": 12, "low": 10, "close": 12, "volume": 100},  # closed on the high
        {"ts": 60, "open": 12, "high": 12, "low": 10, "close": 10, "volume": 100},  # closed on the low
        {"ts": 120, "open": 11, "high": 12, "low": 10, "close": 11, "volume": 100},  # middle
    ])
    assert bars[0]["buy_vol"] == 100 and bars[0]["delta"] == 100
    assert bars[1]["sell_vol"] == 100 and bars[1]["delta"] == -100
    assert bars[2]["buy_vol"] == pytest.approx(50)
    assert all(b["estimated"] for b in bars), "estimates must be labelled"


def test_a_doji_with_no_range_is_neutral():
    bars = estimate_bars([{"ts": 0, "open": 10, "high": 10, "low": 10, "close": 10, "volume": 50}])
    assert bars[0]["delta"] == 0 and bars[0]["buy_vol"] == 25


# --------------------------------------------------------------------------- #
# dots
# --------------------------------------------------------------------------- #


def bar(ts, o, h, l, c, buy, sell, levels=None):
    vol = buy + sell
    return {
        "ts": ts, "open": o, "high": h, "low": l, "close": c,
        "volume": vol, "buy_vol": buy, "sell_vol": sell, "delta": buy - sell,
        "delta_pct": (buy - sell) / vol if vol else 0.0,
        "trades": 10, "vwap": (h + l + c) / 3, "poc": c,
        "venues": {}, "levels": levels or [], "estimated": False,
    }


def test_dots_size_by_volume_and_colour_by_delta():
    bars = [bar(i * 60, 100, 101, 99, 100.5, 5 + i, 5) for i in range(10)]
    bars.append(bar(600, 100, 101, 99, 100.5, 200, 10))   # a monster
    out = build_dots(bars)
    dots = out["dots"]
    assert len(dots) == 11
    assert dots[-1]["size"] > dots[0]["size"], "the big bar gets the big dot"
    assert all(1 <= d["size"] <= 5 for d in dots)
    assert dots[-1]["delta"] > 0
    assert out["cvd"][-1]["value"] == pytest.approx(sum(b["delta"] for b in bars))
    assert out["cvd_total"] == out["cvd"][-1]["value"]


def test_absorption_is_named():
    """Heavy one-sided buying that produces no candle is someone selling into it."""
    bars = [bar(i * 60, 100, 100.5, 99.5, 100.02, 90, 10) for i in range(3)]
    dots = build_dots(bars)["dots"]
    assert all(d["kind"] == "absorption" for d in dots)
    assert "absorbed" in dots[0]["note"]


def test_divergence_is_named():
    b = bar(0, 100, 101, 99, 99.2, 90, 10)   # buyers aggressive, price fell
    dots = build_dots([b])["dots"]
    assert dots[0]["kind"] == "divergence"


def test_initiative_is_named():
    small = [bar(i * 60, 100, 100.2, 99.8, 100, 20, 20) for i in range(6)]
    push = bar(600, 100, 104, 99.9, 103.8, 95, 5)
    dots = build_dots(small + [push])["dots"]
    assert dots[-1]["kind"] == "initiative"
    assert "paid up" in dots[-1]["note"]


def test_thin_moves_are_flagged():
    bars = [bar(i * 60, 100, 102, 100, 101.9, 50, 50) for i in range(3)]
    dots = build_dots(bars)["dots"]
    assert dots[0]["kind"] == "thin"


def test_point_of_control_comes_from_the_levels():
    levels = [{"price": 100.0, "buy": 5, "sell": 5}, {"price": 101.0, "buy": 40, "sell": 40}]
    out = build_dots([bar(0, 100, 101, 99, 100.5, 45, 45, levels)])
    assert out["poc"] == 101.0
    assert out["levels"][0]["price"] == 101.0


def test_no_bars_no_crash():
    out = build_dots([])
    assert out["dots"] == [] and out["cvd"] == [] and out["poc"] is None


# --------------------------------------------------------------------------- #
# the read
# --------------------------------------------------------------------------- #


def read_of(bars):
    built = build_dots(bars)
    return read_flow(bars, built["dots"], built["cvd"], venue_split(bars))


def test_read_needs_tape():
    out = read_of([bar(0, 100, 101, 99, 100, 10, 10)])
    assert out["ok"] is False and out["direction"] == "flat"


def test_steady_buying_reads_bullish():
    bars = [bar(i * 60, 100 + i, 101 + i, 99 + i, 100.9 + i, 80, 20) for i in range(20)]
    out = read_of(bars)
    assert out["ok"] and out["direction"] == "up"
    assert out["score"] > 12 and out["confidence"] > 0
    assert any("buying" in r["text"] for r in out["reasons"])
    assert out["delta_tilt"] > 0


def test_steady_selling_reads_bearish():
    bars = [bar(i * 60, 100 - i, 101 - i, 99 - i, 99.1 - i, 20, 80) for i in range(20)]
    out = read_of(bars)
    assert out["direction"] == "down" and out["score"] < -12


def test_a_rally_nobody_paid_for_is_a_warning():
    """Price grinds up while cumulative delta grinds down — the classic top tell."""
    bars = [bar(i * 60, 100 + i * 0.2, 100.4 + i * 0.2, 99.9 + i * 0.2, 100.3 + i * 0.2, 30, 70)
            for i in range(20)]
    out = read_of(bars)
    assert out["direction"] == "down"
    assert any("disagree" in r["text"] for r in out["reasons"])
    assert out["price_slope"] > 0 > out["cvd_slope"]


def test_balanced_tape_gives_no_signal():
    bars = [bar(i * 60, 100, 100.4, 99.6, 100 + (0.05 if i % 2 else -0.05), 50, 50) for i in range(20)]
    out = read_of(bars)
    assert out["direction"] == "flat"
    assert abs(out["score"]) < 12


def test_every_exchange_agreeing_counts_for_something():
    def with_venues(i):
        b = bar(i * 60, 100 + i, 101 + i, 99 + i, 100.9 + i, 60, 40)
        b["venues"] = {"binance": {"buy": 30, "sell": 20}, "okx": {"buy": 20, "sell": 10},
                       "bybit": {"buy": 10, "sell": 10}}
        return b
    bars = [with_venues(i) for i in range(20)]
    built = build_dots(bars)
    venues = venue_split(bars)
    assert [v["venue"] for v in venues][0] == "binance", "sorted by volume"
    out = read_flow(bars, built["dots"], built["cvd"], venues)
    assert any("exchange" in r["text"] for r in out["reasons"])


def test_venues_pulling_apart_is_reported():
    def split(i):
        b = bar(i * 60, 100, 101, 99, 100, 55, 45)
        b["venues"] = {"binance": {"buy": 50, "sell": 10}, "okx": {"buy": 5, "sell": 35}}
        return b
    bars = [split(i) for i in range(20)]
    built = build_dots(bars)
    out = read_flow(bars, built["dots"], built["cvd"], venue_split(bars))
    assert any("disagree" in r["text"] for r in out["reasons"])


def test_big_prints_tilt_the_read():
    bars = [bar(i * 60, 100, 101, 99, 100, 50, 50) for i in range(20)]
    built = build_dots(bars)
    prints = [{"side": "sell", "notional": 900_000.0, "qty": 9, "price": 100, "venue": "binance", "ts": 0}]
    out = read_flow(bars, built["dots"], built["cvd"], [], prints)
    assert any("large prints" in r["text"] for r in out["reasons"])
    assert out["score"] < 0


def test_reasons_are_sorted_by_weight_and_readable():
    bars = [bar(i * 60, 100 + i, 101 + i, 99 + i, 100.9 + i, 85, 15) for i in range(20)]
    out = read_of(bars)
    weights = [abs(r["weight"]) for r in out["reasons"]]
    assert weights == sorted(weights, reverse=True)
    for r in out["reasons"]:
        assert r["text"] and isinstance(r["bull"], bool)
    assert out["headline"] and "—" in out["headline"]


def test_estimates_are_carried_through_to_the_read():
    rows = estimate_bars([{"ts": i * 60, "open": 100, "high": 101, "low": 99, "close": 101, "volume": 10}
                          for i in range(20)])
    built = build_dots(rows)
    assert all(d["estimated"] for d in built["dots"])
    out = read_flow(rows, built["dots"], built["cvd"], [])
    assert out["estimated"] is True, "the UI has to be able to say this is not real tape"


def test_summary_totals():
    bars = [bar(i * 60, 100, 101, 99, 100, 30, 20) for i in range(5)]
    s = summarise(bars)
    assert s["bars"] == 5 and s["volume"] == 250
    assert s["delta"] == 50 and s["buy_bars"] == 5 and s["sell_bars"] == 0
    assert s["delta_pct"] == pytest.approx(0.2)
    assert summarise([]) == {}
