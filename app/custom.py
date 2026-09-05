"""Custom strategy builder.

Users compose strategies out of indicator rules (no code) in the dashboard.
Specs are validated, persisted to ``data/custom_strategies.json`` and compiled
into real :class:`app.strategies.Strategy` objects that trade inside the same
ensemble as the built-ins.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from app.config import ROOT
from app.indicators import RollingWindow, clamp
from app.models import Signal, SignalKind
from app.rules import (
    FRAMES,
    MIN_BARS,
    context_at,
    count_conditions,
    evaluate_rule,
    validate_rule,
)
from app.strategies import Strategy

log = logging.getLogger("custom")

STORE_PATH = ROOT / "data" / "custom_strategies.json"

SIDES = ("long", "short", "both")


# --------------------------------------------------------------------------- #
# spec handling
# --------------------------------------------------------------------------- #


def new_id() -> str:
    return "cs_" + uuid.uuid4().hex[:8]


def normalize_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Fill defaults + coerce types so a half-built UI payload is safe."""
    out = dict(spec or {})
    out["id"] = str(out.get("id") or new_id())
    out["name"] = (str(out.get("name") or "Untitled strategy")).strip()[:60]
    out["description"] = (str(out.get("description") or "")).strip()[:240]
    out["enabled"] = bool(out.get("enabled", True))
    out["weight"] = round(float(out.get("weight", 1.0) or 1.0), 3)
    out["confidence"] = round(float(out.get("confidence", 0.66) or 0.66), 3)
    side = str(out.get("side") or "long").lower()
    out["side"] = side if side in SIDES else "long"
    out["entry"] = out.get("entry") or {"op": "all", "rules": []}
    out["exit"] = out.get("exit") or None
    out["short_entry"] = out.get("short_entry") or None
    out["symbols"] = [
        str(s).upper().replace("-", "/") for s in (out.get("symbols") or []) if str(s).strip()
    ][:40]
    out["cooldown_sec"] = max(0.0, float(out.get("cooldown_sec", 180) or 0))
    out["min_bars"] = int(out.get("min_bars", 40) or 40)
    for key in ("stop_loss_pct", "take_profit_pct", "trail_pct"):
        val = out.get(key)
        out[key] = None if val in (None, "", 0) else round(abs(float(val)), 5)
    out["tags"] = [str(t)[:24] for t in (out.get("tags") or [])][:8]
    out["source"] = out.get("source") or "builder"
    out["created_ts"] = float(out.get("created_ts") or time.time())
    out["updated_ts"] = time.time()
    out["stats"] = out.get("stats") or {}
    return out


def validate_spec(spec: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    spec = spec or {}
    if not str(spec.get("name") or "").strip():
        errors.append("name is required")
    weight = float(spec.get("weight", 1.0) or 1.0)
    if not 0.05 <= weight <= 3.0:
        errors.append("weight must be between 0.05 and 3.0")
    conf = float(spec.get("confidence", 0.66) or 0.66)
    if not 0.1 <= conf <= 1.0:
        errors.append("confidence must be between 0.1 and 1.0")
    entry = spec.get("entry")
    if count_conditions(entry) == 0:
        errors.append("add at least one entry condition")
    errors.extend(validate_rule(entry))
    if spec.get("exit"):
        errors.extend(validate_rule(spec["exit"]))
    if spec.get("short_entry"):
        errors.extend(validate_rule(spec["short_entry"]))
    if count_conditions(entry) > 24:
        errors.append("too many conditions (max 24)")
    return errors


# --------------------------------------------------------------------------- #
# runtime strategy
# --------------------------------------------------------------------------- #


class CustomStrategy(Strategy):
    family = "custom"

    def __init__(self, spec: dict[str, Any]):
        self.spec = normalize_spec(spec)
        super().__init__(
            {
                "enabled": self.spec["enabled"],
                "weight": self.spec["weight"],
            }
        )
        self.name = self.spec["id"]
        self.title = self.spec["name"]
        self.custom = True
        self._last_fire: dict[str, float] = {}
        self.last_trace: dict[str, Any] = {}
        self.fires = 0

    # -- helpers ---------------------------------------------------------- #
    @property
    def side(self) -> str:
        return self.spec.get("side", "long")

    def applies_to(self, symbol: str) -> bool:
        syms = self.spec.get("symbols") or []
        return not syms or symbol in syms

    def _cooldown_ok(self, symbol: str) -> bool:
        cd = float(self.spec.get("cooldown_sec") or 0)
        if cd <= 0:
            return True
        return time.time() - self._last_fire.get(symbol, 0.0) >= cd

    def evaluate(self, symbol: str, win: RollingWindow, price: float) -> Signal | None:
        min_bars = max(MIN_BARS, int(self.spec.get("min_bars", 40)))
        if len(win) < min_bars or not self.applies_to(symbol):
            return None
        frame = FRAMES.get(symbol, win)
        if not frame:
            return None
        ctx = context_at(frame, -1, extra={"live_price": price})
        return self.evaluate_ctx(symbol, ctx, price)

    def evaluate_ctx(self, symbol: str, ctx: dict[str, Any], price: float) -> Signal | None:
        conf = float(self.spec.get("confidence", 0.66))
        side = self.side
        entry_ok, entry_trace = evaluate_rule(self.spec.get("entry"), ctx)
        exit_ok, exit_trace = (False, [])
        if self.spec.get("exit"):
            exit_ok, exit_trace = evaluate_rule(self.spec["exit"], ctx)
        short_ok, short_trace = (False, [])
        if self.spec.get("short_entry"):
            short_ok, short_trace = evaluate_rule(self.spec["short_entry"], ctx)
        self.last_trace = {
            "symbol": symbol,
            "entry": entry_trace,
            "exit": exit_trace,
            "short": short_trace,
            "ts": time.time(),
        }
        kind: SignalKind | None = None
        reason = ""
        if exit_ok:
            kind = SignalKind.SELL if side != "short" else SignalKind.BUY
            reason = f"{self.title}: exit rules met"
        elif entry_ok and side in ("long", "both"):
            kind = SignalKind.BUY
            reason = f"{self.title}: {len(entry_trace)} entry rules met"
        elif (short_ok or (entry_ok and side == "short")) and side in ("short", "both"):
            kind = SignalKind.SELL
            reason = f"{self.title}: short rules met"
        if kind is None:
            return None
        if kind in (SignalKind.BUY, SignalKind.SELL) and not self._cooldown_ok(symbol):
            return None
        self._last_fire[symbol] = time.time()
        self.fires += 1
        return self._sig(
            symbol,
            kind,
            conf,
            price,
            reason,
            custom=True,
            spec_id=self.spec["id"],
            stop_loss_pct=self.spec.get("stop_loss_pct"),
            take_profit_pct=self.spec.get("take_profit_pct"),
            trail_pct=self.spec.get("trail_pct"),
            trace=(entry_trace or short_trace or exit_trace)[:8],
        )

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.spec)
        d["enabled"] = self.enabled
        d["weight"] = self.weight
        d["fires"] = self.fires
        d["conditions"] = count_conditions(self.spec.get("entry"))
        return d


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #


class CustomRegistry:
    """CRUD + persistence for builder strategies."""

    def __init__(self, path: Path = STORE_PATH):
        self.path = path
        self.strategies: dict[str, CustomStrategy] = {}
        self.load()

    # persistence ---------------------------------------------------------
    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except Exception as exc:  # pragma: no cover - corrupted file
            log.warning("custom strategy store unreadable: %s", exc)
            return
        for spec in raw.get("strategies", []):
            try:
                strat = CustomStrategy(spec)
                self.strategies[strat.spec["id"]] = strat
            except Exception as exc:
                log.warning("skipping bad custom spec: %s", exc)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "saved_ts": time.time(),
            "strategies": [s.to_dict() for s in self.strategies.values()],
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), "utf-8")
        tmp.replace(self.path)

    # crud ----------------------------------------------------------------
    def list(self) -> list[dict[str, Any]]:
        return sorted(
            (s.to_dict() for s in self.strategies.values()),
            key=lambda d: d.get("updated_ts", 0),
            reverse=True,
        )

    def get(self, spec_id: str) -> CustomStrategy | None:
        return self.strategies.get(spec_id)

    def upsert(self, spec: dict[str, Any]) -> tuple[CustomStrategy | None, list[str]]:
        errors = validate_spec(spec)
        if errors:
            return None, errors
        normalized = normalize_spec(spec)
        existing = self.strategies.get(normalized["id"])
        if existing:
            normalized["created_ts"] = existing.spec.get("created_ts", normalized["created_ts"])
        strat = CustomStrategy(normalized)
        self.strategies[strat.spec["id"]] = strat
        self.save()
        return strat, []

    def delete(self, spec_id: str) -> bool:
        if spec_id in self.strategies:
            del self.strategies[spec_id]
            self.save()
            return True
        return False

    def toggle(self, spec_id: str, enabled: bool | None = None) -> bool | None:
        strat = self.strategies.get(spec_id)
        if not strat:
            return None
        strat.enabled = (not strat.enabled) if enabled is None else bool(enabled)
        strat.spec["enabled"] = strat.enabled
        self.save()
        return strat.enabled

    def duplicate(self, spec_id: str) -> CustomStrategy | None:
        src = self.strategies.get(spec_id)
        if not src:
            return None
        spec = dict(src.spec)
        spec["id"] = new_id()
        spec["name"] = f"{spec['name']} copy"
        strat, _ = self.upsert(spec)
        return strat

    def active(self) -> list[CustomStrategy]:
        return [s for s in self.strategies.values() if s.enabled]

    def all(self) -> list[CustomStrategy]:
        return list(self.strategies.values())


# --------------------------------------------------------------------------- #
# starter templates
# --------------------------------------------------------------------------- #

TEMPLATES: list[dict[str, Any]] = [
    {
        "template_id": "squeeze_break",
        "name": "Squeeze breakout",
        "description": "Bollinger inside Keltner, then a volume-backed push through the 20-bar high.",
        "tags": ["breakout", "volatility"],
        "side": "long",
        "confidence": 0.72,
        "weight": 1.1,
        "stop_loss_pct": 0.018,
        "take_profit_pct": 0.045,
        "trail_pct": 0.012,
        "entry": {
            "op": "all",
            "rules": [
                {"left": "squeeze", "cmp": "is_true"},
                {"left": "close", "cmp": ">=", "right": "hh20"},
                {"left": "vol_ratio", "cmp": ">", "right": 1.6},
                {"left": "adx", "cmp": ">", "right": 18},
            ],
        },
        "exit": {"op": "any", "rules": [{"left": "close", "cmp": "cross_below", "right": "ema21"}]},
    },
    {
        "template_id": "trend_pullback",
        "name": "Trend pullback",
        "description": "Buy the dip inside a confirmed uptrend — EMA stack up, RSI cooling off.",
        "tags": ["trend", "pullback"],
        "side": "long",
        "confidence": 0.68,
        "weight": 1.0,
        "stop_loss_pct": 0.02,
        "take_profit_pct": 0.04,
        "entry": {
            "op": "all",
            "rules": [
                {"left": "ema_stack", "cmp": "==", "right": 1},
                {"left": "close", "cmp": ">", "right": "ema50"},
                {"left": "rsi", "cmp": "between", "right": [38, 52]},
                {"left": "adx", "cmp": ">", "right": 20},
            ],
        },
        "exit": {"op": "any", "rules": [{"left": "rsi", "cmp": ">", "right": 74}]},
    },
    {
        "template_id": "macd_momentum",
        "name": "MACD momentum cross",
        "description": "MACD crosses its signal above zero while volume expands.",
        "tags": ["momentum"],
        "side": "long",
        "confidence": 0.65,
        "weight": 1.0,
        "entry": {
            "op": "all",
            "rules": [
                {"left": "macd", "cmp": "cross_above", "right": "macd_signal"},
                {"left": "macd_hist", "cmp": ">", "right": 0},
                {"left": "vol_ratio", "cmp": ">", "right": 1.1},
            ],
        },
        "exit": {"op": "any", "rules": [{"left": "macd", "cmp": "cross_below", "right": "macd_signal"}]},
    },
    {
        "template_id": "mean_revert",
        "name": "Panic mean reversion",
        "description": "Deep z-score flush with an oversold RSI and a lower wick rejection.",
        "tags": ["mean-reversion"],
        "side": "long",
        "confidence": 0.63,
        "weight": 0.9,
        "stop_loss_pct": 0.025,
        "take_profit_pct": 0.03,
        "entry": {
            "op": "all",
            "rules": [
                {"left": "zscore", "cmp": "<", "right": -1.8},
                {"left": "rsi", "cmp": "<", "right": 30},
                {"left": "lower_wick_pct", "cmp": ">", "right": 35},
            ],
        },
        "exit": {"op": "any", "rules": [{"left": "close", "cmp": ">=", "right": "bb_mid"}]},
    },
    {
        "template_id": "vwap_reclaim",
        "name": "VWAP reclaim",
        "description": "Price reclaims session VWAP with positive OBV slope.",
        "tags": ["flow", "intraday"],
        "side": "long",
        "confidence": 0.62,
        "weight": 0.95,
        "entry": {
            "op": "all",
            "rules": [
                {"left": "close", "cmp": "cross_above", "right": "vwap"},
                {"left": "obv_slope", "cmp": ">", "right": 0},
                {"left": "mom_score", "cmp": ">", "right": 55},
            ],
        },
    },
    {
        "template_id": "supertrend_flip",
        "name": "Supertrend flip + ADX",
        "description": "Supertrend flips bullish while ADX confirms a real regime.",
        "tags": ["trend"],
        "side": "long",
        "confidence": 0.7,
        "weight": 1.05,
        "entry": {
            "op": "all",
            "rules": [
                {"left": "st_dir", "cmp": ">", "right": 0},
                {"left": "adx", "cmp": ">", "right": 22},
                {"left": "plus_di", "cmp": ">", "right": "minus_di"},
                {"left": "close", "cmp": ">", "right": "ema21"},
            ],
        },
        "exit": {"op": "any", "rules": [{"left": "st_dir", "cmp": "<", "right": 0}]},
    },
    {
        "template_id": "fade_blowoff",
        "name": "Fade the blow-off",
        "description": "Short exhaustion: stretched above the upper band with a fading histogram.",
        "tags": ["mean-reversion", "short"],
        "side": "short",
        "confidence": 0.6,
        "weight": 0.85,
        "entry": {
            "op": "all",
            "rules": [
                {"left": "bb_pct", "cmp": ">", "right": 1.0},
                {"left": "rsi", "cmp": ">", "right": 76},
                {"left": "macd_hist", "cmp": "falling"},
            ],
        },
    },
    {
        "template_id": "volume_thrust",
        "name": "Volume thrust",
        "description": "Volume z-score spike with a wide-range bar closing near its high.",
        "tags": ["flow", "breakout"],
        "side": "long",
        "confidence": 0.64,
        "weight": 0.95,
        "entry": {
            "op": "all",
            "rules": [
                {"left": "vol_z", "cmp": ">", "right": 2.0},
                {"left": "buy_pressure", "cmp": ">", "right": 70},
                {"left": "range_pct", "cmp": ">", "right": 0.4},
                {"left": "close", "cmp": ">", "right": "ema9"},
            ],
        },
    },
    {
        "template_id": "golden_stack",
        "name": "Golden stack continuation",
        "description": "Long-term trend filter: price over EMA200 with a fresh 50/200 alignment.",
        "tags": ["trend", "swing"],
        "side": "long",
        "confidence": 0.66,
        "weight": 1.0,
        "entry": {
            "op": "all",
            "rules": [
                {"left": "close", "cmp": ">", "right": "ema200"},
                {"left": "ema50", "cmp": ">", "right": "ema200"},
                {"left": "trend_score", "cmp": ">", "right": 62},
                {"left": "dist_hh_pct", "cmp": "<", "right": 3},
            ],
        },
    },
]


def template(template_id: str) -> dict[str, Any] | None:
    for t in TEMPLATES:
        if t["template_id"] == template_id:
            spec = dict(t)
            spec.pop("template_id", None)
            spec["id"] = new_id()
            spec["source"] = f"template:{template_id}"
            return normalize_spec(spec)
    return None
