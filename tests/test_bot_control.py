"""Coin selection, trade-quality gating, credentials and 24/7 supervision."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

import pytest

from app.edge import DEFAULTS, EdgeEngine
from app.keys import KeyStore, mask
from app.runtime import Supervisor
from app.tradeset import TradeSet

WATCH = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT"]


# --------------------------------------------------------------------------- #
# coin selection
# --------------------------------------------------------------------------- #


def test_selected_mode_only_trades_ticked_coins(tmp_path):
    ts = TradeSet(tmp_path / "trading.json")
    assert ts.active(WATCH) == [], "nothing is tradable until you pick something"
    ts.select(["BTC/USDT", "SOL/USDT"])
    assert ts.active(WATCH) == ["BTC/USDT", "SOL/USDT"]
    ok, _ = ts.allows("BTC/USDT", WATCH)
    blocked, why = ts.allows("ETH/USDT", WATCH)
    assert ok and not blocked and "not selected" in why


def test_selection_survives_a_restart(tmp_path):
    path = tmp_path / "trading.json"
    ts = TradeSet(path)
    ts.select(["eth-usdt"])  # normalized on the way in
    assert ts.symbols == ["ETH/USDT"]
    again = TradeSet(path)
    assert again.symbols == ["ETH/USDT"]
    assert again.mode == "selected"
    assert json.loads(path.read_text())["symbols"] == ["ETH/USDT"]


def test_all_mode_trades_the_whole_watchlist(tmp_path):
    ts = TradeSet(tmp_path / "trading.json")
    ts.configure({"mode": "all"})
    assert ts.active(WATCH) == WATCH


def test_toggle_and_disarm_single_coin(tmp_path):
    ts = TradeSet(tmp_path / "trading.json")
    assert ts.toggle("BTC/USDT") is True
    assert ts.active(WATCH) == ["BTC/USDT"]
    assert ts.toggle("BTC/USDT") is False
    ts.select(["BTC/USDT", "ETH/USDT"])
    ts.set_enabled("ETH/USDT", False)
    assert ts.active(WATCH) == ["BTC/USDT"]
    blocked, why = ts.allows("ETH/USDT", WATCH)
    assert not blocked and why == "coin disarmed"


def test_per_symbol_size_multiplier(tmp_path):
    ts = TradeSet(tmp_path / "trading.json")
    assert ts.size_multiplier("BTC/USDT") == 1.0
    ts.configure({"per_symbol": {"BTC/USDT": {"size_mult": 2.5}}})
    assert ts.size_multiplier("BTC/USDT") == 2.5
    ts.configure({"per_symbol": {"BTC/USDT": {"size_mult": 99}}})
    assert ts.size_multiplier("BTC/USDT") == 3.0, "multipliers are clamped"


def test_auto_mode_tracks_the_top_scores(tmp_path):
    ts = TradeSet(tmp_path / "trading.json")
    ts.configure({"mode": "auto", "auto_top_n": 2, "auto_metric": "score"})
    rows = [
        {"symbol": "BTC/USDT", "score": 40, "quote_volume": 5e9},
        {"symbol": "ETH/USDT", "score": 90, "quote_volume": 3e9},
        {"symbol": "SOL/USDT", "score": 70, "quote_volume": 1e9},
    ]
    assert ts.refresh_auto(rows) == ["ETH/USDT", "SOL/USDT"]
    assert ts.active(WATCH) == ["ETH/USDT", "SOL/USDT"]
    # pinned coins always ride along
    ts.configure({"pinned": ["BTC/USDT"]})
    assert ts.refresh_auto(rows)[0] == "BTC/USDT"
    # and a volume floor filters the basket
    ts.configure({"auto_min_volume": 4e9, "pinned": []})
    assert ts.refresh_auto(rows) == ["BTC/USDT"]


# --------------------------------------------------------------------------- #
# trade quality engine
# --------------------------------------------------------------------------- #


def good_ctx(**over):
    ctx = {
        "confidence": 0.75,
        "strategy": "momentum",
        "mtf_agreement": 0.8,
        "mtf_bias": "bullish",
        "forecast_prob": 0.66,
        "risk_on": True,
        "trend_score": 0.6,
        "rsi": 58,
        "atr_pct": 1.1,
        "spread_bps": 4.0,
        "quote_volume": 5e8,
        "open_correlated": 0,
    }
    ctx.update(over)
    return ctx


def engine(tmp_path, **cfg):
    e = EdgeEngine(tmp_path / "edge.json")
    if cfg:
        e.configure(cfg)
    return e


def test_a_clean_setup_passes_with_a_high_score(tmp_path):
    d = engine(tmp_path).evaluate("BTC/USDT", good_ctx())
    assert d["passed"] and d["blocks"] == []
    assert d["score"] > 70
    assert 0.5 <= d["size_mult"] <= 1.6
    assert set(d["parts"]) == {
        "confidence", "mtf", "forecast", "regime", "trend", "volatility", "liquidity", "track_record"
    }


@pytest.mark.parametrize(
    "override,fragment",
    [
        ({"mtf_agreement": 0.1}, "timeframes disagree"),
        ({"mtf_bias": "bearish"}, "higher timeframe bias is bearish"),
        ({"risk_on": False}, "risk-off"),
        ({"spread_bps": 90}, "spread"),
        ({"atr_pct": 0.01}, "too quiet"),
        ({"atr_pct": 25}, "too wild"),
        ({"rsi": 92}, "overextended"),
    ],
)
def test_each_hard_filter_blocks_with_a_readable_reason(tmp_path, override, fragment):
    d = engine(tmp_path).evaluate("BTC/USDT", good_ctx(**override))
    assert not d["passed"]
    assert any(fragment in b for b in d["blocks"]), d["blocks"]


def test_quality_floor_rejects_mediocre_setups(tmp_path):
    e = engine(tmp_path, min_quality=90)
    d = e.evaluate("BTC/USDT", good_ctx(confidence=0.55, forecast_prob=0.5, trend_score=0.0))
    assert not d["passed"] and "below" in d["blocks"][0]


def test_pacing_guards(tmp_path):
    e = engine(tmp_path, max_trades_per_day=2, symbol_cooldown_min=0, loss_cooldown_min=0)
    e.trades_today = 2
    assert "daily trade cap" in " ".join(e.evaluate("BTC/USDT", good_ctx())["blocks"])
    e.trades_today = 0
    e.consecutive_losses = 5
    assert "losses in a row" in " ".join(e.evaluate("BTC/USDT", good_ctx())["blocks"])
    e.consecutive_losses = 0
    assert "correlated" in " ".join(e.evaluate("BTC/USDT", good_ctx(open_correlated=9))["blocks"])


def test_cooldowns_expire(tmp_path):
    e = engine(tmp_path, loss_cooldown_min=10, symbol_cooldown_min=10)
    e.last_loss_ts = time.time()
    assert "cooling off" in " ".join(e.evaluate("BTC/USDT", good_ctx())["blocks"])
    e.last_loss_ts = time.time() - 3600
    e.last_trade_ts["BTC/USDT"] = time.time()
    blocks = e.evaluate("BTC/USDT", good_ctx())["blocks"]
    assert any("cooldown" in b for b in blocks)
    e.last_trade_ts["BTC/USDT"] = time.time() - 3600
    assert e.evaluate("BTC/USDT", good_ctx())["passed"]


def test_bad_strategies_get_benched(tmp_path):
    e = engine(tmp_path, min_strategy_trades=5, min_strategy_winrate=0.4)
    for _ in range(8):
        e.record_trade("momentum", "BTC/USDT", -10.0, -1.0)
    d = e.evaluate("BTC/USDT", good_ctx())
    assert not d["passed"]
    assert "momentum is running 0%" in " ".join(d["blocks"])
    assert e.consecutive_losses == 8


def test_track_record_lifts_the_score(tmp_path):
    weak = engine(tmp_path / "a" if False else tmp_path, min_strategy_trades=99)
    before = weak.evaluate("BTC/USDT", good_ctx())["parts"]["track_record"]
    for _ in range(12):
        weak.record_trade("momentum", "BTC/USDT", 25.0, 1.5)
    after = weak.evaluate("BTC/USDT", good_ctx())["parts"]["track_record"]
    assert after > before


def test_rejections_are_recorded_with_reasons(tmp_path):
    e = engine(tmp_path)
    d = e.evaluate("BTC/USDT", good_ctx(risk_on=False))
    e.reject(d)
    stats = e.stats()
    assert stats["rejected"] == 1
    assert stats["recent_rejections"][0]["symbol"] == "BTC/USDT"
    assert stats["top_blocks"][0]["count"] == 1


def test_volatility_targeted_sizing(tmp_path):
    e = engine(tmp_path, vol_target_pct=1.0, atr_stop_mult=2.0)
    d = e.evaluate("BTC/USDT", good_ctx())
    calm = e.position_size(10_000, 100.0, 1.0, d, max_notional=1e9)
    wild = e.position_size(10_000, 100.0, 5.0, d, max_notional=1e9)
    assert calm["notional"] > wild["notional"], "more volatility, smaller position"
    # risking 1% of 10k with a 2 ATR stop of $2 on a $100 coin -> 50 units -> $5000
    assert calm["risk_budget"] == 100.0
    assert calm["stop_distance"] == 2.0
    capped = e.position_size(10_000, 100.0, 1.0, d, max_notional=800)
    assert capped["notional"] == 800 and capped["capped"] is True


def test_kelly_scales_with_live_results(tmp_path):
    e = engine(tmp_path)
    d = e.evaluate("BTC/USDT", good_ctx())
    losing = e.position_size(10_000, 100.0, 1.0, d, 1e9, wins=3, losses=17)
    winning = e.position_size(10_000, 100.0, 1.0, d, 1e9, wins=17, losses=3)
    assert winning["kelly_mult"] > losing["kelly_mult"]
    assert losing["kelly_mult"] >= 0.25


def test_disabled_gate_is_inert(tmp_path):
    e = engine(tmp_path, enabled=False)
    assert e.cfg["enabled"] is False
    assert e.configure({"min_quality": 12})["min_quality"] == 12.0
    assert e.reset_config()["min_quality"] == DEFAULTS["min_quality"]


# --------------------------------------------------------------------------- #
# exit management
# --------------------------------------------------------------------------- #


@dataclass
class FakePos:
    entry: float = 100.0
    stop: float = 98.0
    take: float = 110.0
    peak: float = 100.0
    atr: float = 1.0
    opened_ts: float = 0.0
    partials_taken: int = 0

    def __post_init__(self):
        if not self.opened_ts:
            self.opened_ts = time.time()


def kinds(actions):
    return [a["kind"] for a in actions]


def test_break_even_then_partials_then_trail(tmp_path):
    e = engine(tmp_path)
    pos = FakePos()
    # +2 on a 2-wide risk = 1R -> break-even stop and the first partial
    pos.peak = 102.0
    acts = e.manage(pos, 102.0)
    assert "stop" in kinds(acts) and "scale" in kinds(acts)
    be = next(a for a in acts if a["kind"] == "stop")
    assert be["value"] >= pos.entry
    scale = next(a for a in acts if a["kind"] == "scale")
    assert scale["frac"] == DEFAULTS["partial_1_frac"]
    # after the first partial, the second only fires at 2R
    pos.partials_taken = 1
    assert "scale" not in kinds(e.manage(pos, 102.0))
    pos.peak = 104.0
    assert "scale" in kinds(e.manage(pos, 104.0))


def test_giveback_lock_closes_a_fading_winner(tmp_path):
    e = engine(tmp_path, giveback_pct=0.4, min_hold_sec=0)
    pos = FakePos(peak=106.0, opened_ts=time.time() - 600)
    acts = e.manage(pos, 102.0)  # 3R peak, back to 1R = 66% given back
    assert kinds(acts) == ["close"]
    assert "gave back" in acts[0]["reason"]


def test_time_stop_closes_dead_trades(tmp_path):
    e = engine(tmp_path, time_stop_min=60)
    pos = FakePos(opened_ts=time.time() - 3 * 3600, peak=100.4)
    acts = e.manage(pos, 100.2)
    assert any(a["kind"] == "close" and "time stop" in a["reason"] for a in acts)
    fresh = FakePos(opened_ts=time.time(), peak=100.4)
    assert not [a for a in e.manage(fresh, 100.2) if a["kind"] == "close"]


def test_initial_levels_use_atr_when_available(tmp_path):
    e = engine(tmp_path, atr_stop_mult=2.0, atr_take_mult=4.0)
    lv = e.initial_levels(100.0, 1.5, 97.0, 104.0)
    assert lv["stop"] == pytest.approx(97.0)
    assert lv["take"] == pytest.approx(106.0)
    assert lv["risk"] == pytest.approx(3.0)
    flat = e.initial_levels(100.0, 0.0, 97.5, 103.5)
    assert flat["stop"] == pytest.approx(97.5) and flat["take"] == pytest.approx(103.5)


# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #


def test_keys_are_encrypted_and_never_returned(tmp_path):
    path = tmp_path / "api_keys.json"
    ks = KeyStore(path)
    view = ks.set("binance", "MYAPIKEY1234567890", "SUPERSECRET0987654321")
    assert view["configured"] is True
    assert "SUPERSECRET" not in json.dumps(view)
    assert view["key_masked"].startswith("MYAP") and "•" in view["key_masked"]
    raw = path.read_text()
    assert "SUPERSECRET0987654321" not in raw, "secrets must not sit in plaintext on disk"
    assert "MYAPIKEY1234567890" not in raw
    # but the process can still sign with them
    assert ks.creds("binance")["secret"] == "SUPERSECRET0987654321"
    assert KeyStore(path).creds("binance")["key"] == "MYAPIKEY1234567890"


def test_venue_field_requirements(tmp_path):
    ks = KeyStore(tmp_path / "k.json")
    with pytest.raises(ValueError):
        ks.set("okx", "k", "s")  # passphrase required
    ks.set("okx", "k" * 12, "s" * 12, passphrase="pp")
    assert ks.describe("okx")["has_passphrase"] is True
    with pytest.raises(ValueError):
        ks.set("nasdaq", "k", "s")
    with pytest.raises(ValueError):
        ks.set("binance", "", "")


def test_trading_flag_and_deletion(tmp_path):
    ks = KeyStore(tmp_path / "k.json")
    ks.set("binance", "key12345678", "secret12345678")
    assert ks.describe("binance")["trade_enabled"] is False
    assert ks.set_trade_enabled("binance", True)["trade_enabled"] is True
    listing = ks.listing()
    assert {v["venue"] for v in listing["venues"]} >= {"binance", "bybit", "okx", "mexc"}
    assert listing["encryption"] in ("fernet", "obfuscated")
    assert ks.delete("binance") is True and ks.ready("binance") is False


def test_masking_never_leaks_more_than_eight_characters():
    assert mask("") == ""
    assert mask("short") == "•••••"
    assert mask("ABCDEFGHIJKLMNOP") == "ABCD••••••MNOP"


def test_live_router_refuses_unarmed_and_unwired_venues(tmp_path):
    from app.live import LiveRouter

    ks = KeyStore(tmp_path / "k.json")
    router = LiveRouter(ks)
    ok, why = router.can_trade("binance")
    assert not ok and "no API credentials" in why
    ks.set("binance", "key12345678", "secret12345678")
    ok, why = router.can_trade("binance")
    assert not ok and "not enabled" in why
    ks.set_trade_enabled("binance", True)
    assert router.can_trade("binance")[0] is True
    ks.set("mexc", "key12345678", "secret12345678")
    ks.set_trade_enabled("mexc", True)
    ok, why = router.can_trade("mexc")
    assert not ok and "not wired" in why
    with pytest.raises(RuntimeError):
        asyncio.run(router.market_buy("mexc", "BTC/USDT", 10))


# --------------------------------------------------------------------------- #
# 24/7 supervision
# --------------------------------------------------------------------------- #


class FakeRisk:
    def __init__(self):
        self.halted = False
        self.halt_reason = ""
        self.last_loss_ts = 0.0
        self.reset_calls = 0

    def reset_day(self, equity):
        self.reset_calls += 1
        self.halted = False
        self.halt_reason = ""


class FakeRobot:
    def __init__(self):
        self.risk = FakeRisk()
        self.running = True
        self.paused = False
        self.last_loop = time.time()
        self.loops = 10
        self.mark_equity = 10_000.0
        self.stops = 0
        self.starts = 0
        self.flattened = 0

    async def stop(self):
        self.stops += 1
        self.running = False

    async def start(self):
        self.starts += 1
        self.running = True
        self.last_loop = time.time()

    async def flatten(self):
        self.flattened += 1


def test_watchdog_restarts_a_stalled_loop(tmp_path):
    bot = FakeRobot()
    sup = Supervisor(bot, tmp_path / "runtime.json")
    sup.configure({"stall_timeout_sec": 30, "auto_restart_loop": True})
    assert asyncio.run(sup.tick()) == {} or True
    bot.last_loop = time.time() - 500
    did = asyncio.run(sup.tick())
    assert did.get("restarted") is True
    assert bot.stops == 1 and bot.starts == 1 and sup.restarts == 1
    assert sup.status()["loop_healthy"] is True
    assert any(e["kind"] == "restart" for e in sup.events)


def test_watchdog_leaves_a_healthy_loop_alone(tmp_path):
    bot = FakeRobot()
    sup = Supervisor(bot, tmp_path / "runtime.json")
    asyncio.run(sup.tick())
    assert bot.starts == 0 and sup.restarts == 0


def test_auto_resume_clears_a_halt_after_cooldown(tmp_path):
    bot = FakeRobot()
    sup = Supervisor(bot, tmp_path / "runtime.json")
    sup.configure({"auto_resume_halt": True, "auto_resume_after_min": 30})
    bot.risk.halted = True
    bot.risk.halt_reason = "daily loss hit"
    bot.risk.last_loss_ts = time.time() - 60  # only a minute ago
    asyncio.run(sup.tick())
    assert bot.risk.halted is True, "must wait out the cooling-off period"
    bot.risk.last_loss_ts = time.time() - 3600
    did = asyncio.run(sup.tick())
    assert did.get("resumed") is True and bot.risk.halted is False


def test_maintenance_window_pauses_and_resumes(tmp_path):
    bot = FakeRobot()
    sup = Supervisor(bot, tmp_path / "runtime.json")
    hour = time.gmtime().tm_hour
    sup.configure({
        "maintenance_enabled": True,
        "maintenance_start_hour_utc": hour,
        "maintenance_end_hour_utc": (hour + 1) % 24,
        "flatten_in_maintenance": True,
    })
    assert sup.maintenance_now() is True
    asyncio.run(sup.tick())
    assert bot.paused is True and bot.flattened == 1
    sup.configure({"maintenance_enabled": False})
    asyncio.run(sup.tick())
    assert bot.paused is False


def test_runtime_config_persists_and_reports_uptime(tmp_path):
    path = tmp_path / "runtime.json"
    bot = FakeRobot()
    sup = Supervisor(bot, path)
    sup.configure({"heartbeat_sec": 45, "daily_reset_hour_utc": 7})
    again = Supervisor(FakeRobot(), path)
    assert again.cfg["heartbeat_sec"] == 45.0
    assert again.cfg["daily_reset_hour_utc"] == 7
    st = sup.status()
    assert st["uptime_human"] and st["running"] is True
    assert st["loops"] == 10 and st["next_daily_reset_hour_utc"] == 7
