"""Trade-quality engine: what to take, how big, and when to get out.

The strategy ensemble decides *if* something looks interesting. This module
decides whether it is worth risking money on, how much to risk, and how the
position is managed afterwards. Three parts:

1. **Entry gate** — every candidate is scored 0-100 from confluence (higher
   timeframe agreement, forecast edge, regime, volatility, spread, liquidity)
   and from how that strategy and that coin have actually performed. Anything
   below ``min_quality``, or tripping a hard block, is rejected *with a reason*
   so you can see why the bot is not trading.
2. **Sizing** — volatility targeting (risk a fixed % of equity per trade based
   on ATR, not a fixed notional) scaled by quality and a capped Kelly fraction
   from live results.
3. **Exits** — ATR stop, break-even move, a partial-profit ladder, ATR trailing,
   a giveback lock and a time stop.

Everything is configurable and persisted, and every rejection is counted so the
settings can be tuned against reality instead of vibes.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Any

log = logging.getLogger("edge")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # --- entry gate ---
    "min_quality": 55.0,
    "require_mtf": True,
    "min_mtf_agreement": 0.40,
    "block_htf_downtrend": True,
    "require_forecast": False,
    "min_forecast_prob": 0.55,
    "regime_filter": True,
    "max_spread_bps": 20.0,
    "min_atr_pct": 0.12,
    "max_atr_pct": 6.0,
    "max_rsi": 78.0,
    "min_quote_volume": 0.0,
    "max_open_correlated": 3,
    "max_trades_per_day": 40,
    "max_consecutive_losses": 4,
    "loss_cooldown_min": 15.0,
    "symbol_cooldown_min": 30.0,
    "session_hours": [],  # empty = trade every hour (UTC)
    "min_strategy_trades": 6,
    "min_strategy_winrate": 0.35,
    "adaptive_weights": True,
    # --- sizing ---
    "vol_target_pct": 0.8,
    "kelly_cap": 0.6,
    "quality_size_floor": 0.5,
    "quality_size_ceiling": 1.6,
    # --- exits ---
    "atr_stop_mult": 1.6,
    "atr_take_mult": 3.2,
    "atr_trail_mult": 1.2,
    "breakeven_at_r": 0.8,
    "partial_1_r": 1.0,
    "partial_1_frac": 0.4,
    "partial_2_r": 2.0,
    "partial_2_frac": 0.3,
    "giveback_pct": 0.40,
    "time_stop_min": 300.0,
    "min_hold_sec": 45.0,
}

# how each confluence factor contributes to the 0-100 quality score
WEIGHTS = {
    "confidence": 22.0,
    "mtf": 20.0,
    "forecast": 16.0,
    "regime": 10.0,
    "trend": 12.0,
    "volatility": 8.0,
    "liquidity": 6.0,
    "track_record": 16.0,
}


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class EdgeEngine:
    def __init__(self, path: Path):
        self.path = path
        self.cfg: dict[str, Any] = dict(DEFAULTS)
        self.by_strategy: dict[str, dict[str, float]] = {}
        self.by_symbol: dict[str, dict[str, float]] = {}
        self.rejections: deque[dict[str, Any]] = deque(maxlen=120)
        self.reject_counts: dict[str, int] = {}
        self.accepted = 0
        self.consecutive_losses = 0
        self.trades_today = 0
        self.day_stamp = time.strftime("%Y-%m-%d", time.gmtime())
        self.last_trade_ts: dict[str, float] = {}
        self.last_loss_ts = 0.0
        self.load()

    # ------------------------------------------------------------------ io
    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except Exception:
            return
        cfg = raw.get("cfg") if isinstance(raw, dict) else None
        if isinstance(cfg, dict):
            self.cfg.update({k: v for k, v in cfg.items() if k in DEFAULTS})
        self.by_strategy = raw.get("by_strategy") or {}
        self.by_symbol = raw.get("by_symbol") or {}

    def save(self) -> None:
        payload = {"cfg": self.cfg, "by_strategy": self.by_strategy, "by_symbol": self.by_symbol}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), "utf-8")
            os.replace(tmp, self.path)
        except Exception as exc:  # pragma: no cover - disk only
            log.warning("could not persist edge config: %s", exc)

    def configure(self, patch: dict[str, Any]) -> dict[str, Any]:
        for key, value in (patch or {}).items():
            if key not in DEFAULTS:
                continue
            default = DEFAULTS[key]
            try:
                if isinstance(default, bool):
                    self.cfg[key] = bool(value)
                elif isinstance(default, list):
                    self.cfg[key] = [int(v) % 24 for v in (value or [])]
                else:
                    self.cfg[key] = float(value)
            except (TypeError, ValueError):
                continue
        self.save()
        return dict(self.cfg)

    def reset_config(self) -> dict[str, Any]:
        self.cfg = dict(DEFAULTS)
        self.save()
        return dict(self.cfg)

    # -------------------------------------------------------------- results
    def record_trade(self, strategy: str, symbol: str, pnl: float, r_multiple: float = 0.0) -> None:
        for book, key in ((self.by_strategy, strategy or "?"), (self.by_symbol, symbol or "?")):
            row = book.setdefault(key, {"n": 0, "wins": 0, "pnl": 0.0, "r": 0.0})
            row["n"] += 1
            row["wins"] += 1 if pnl > 0 else 0
            row["pnl"] += pnl
            row["r"] += r_multiple
        if pnl > 0:
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
            self.last_loss_ts = time.time()
        self.last_trade_ts[(symbol or "").upper()] = time.time()
        self.save()

    def note_entry(self, symbol: str) -> None:
        self._roll_day()
        self.trades_today += 1
        self.accepted += 1
        self.last_trade_ts[(symbol or "").upper()] = time.time()

    def _roll_day(self) -> None:
        stamp = time.strftime("%Y-%m-%d", time.gmtime())
        if stamp != self.day_stamp:
            self.day_stamp = stamp
            self.trades_today = 0

    def win_rate(self, book: dict[str, dict[str, float]], key: str) -> tuple[float, int]:
        row = book.get(key or "?") or {}
        n = int(row.get("n") or 0)
        if not n:
            return 0.5, 0
        return float(row.get("wins") or 0) / n, n

    # ------------------------------------------------------------ entry gate
    def evaluate(self, symbol: str, ctx: dict[str, Any]) -> dict[str, Any]:
        """Score a candidate entry. ``ctx`` is assembled by the engine."""
        cfg = self.cfg
        self._roll_day()
        blocks: list[str] = []
        notes: list[str] = []
        parts: dict[str, float] = {}

        conf = float(ctx.get("confidence") or 0.0)
        parts["confidence"] = WEIGHTS["confidence"] * _clamp((conf - 0.45) / 0.45, 0.0, 1.0)

        # --- higher timeframe confluence
        agreement = ctx.get("mtf_agreement")
        bias = (ctx.get("mtf_bias") or "").lower()
        if agreement is None:
            parts["mtf"] = WEIGHTS["mtf"] * 0.45
            notes.append("no multi-timeframe read yet")
        else:
            parts["mtf"] = WEIGHTS["mtf"] * _clamp(float(agreement), 0.0, 1.0)
            if cfg["require_mtf"] and float(agreement) < cfg["min_mtf_agreement"]:
                blocks.append(f"timeframes disagree ({float(agreement):.2f} < {cfg['min_mtf_agreement']:.2f})")
            if cfg["block_htf_downtrend"] and bias in ("bearish", "down", "short"):
                blocks.append("higher timeframe bias is bearish")

        # --- forecast edge
        prob = ctx.get("forecast_prob")
        if prob is None:
            parts["forecast"] = WEIGHTS["forecast"] * (0.0 if cfg["require_forecast"] else 0.4)
            if cfg["require_forecast"]:
                blocks.append("no forecast available")
        else:
            parts["forecast"] = WEIGHTS["forecast"] * _clamp((float(prob) - 0.45) / 0.35, 0.0, 1.0)
            if float(prob) < cfg["min_forecast_prob"] and cfg["require_forecast"]:
                blocks.append(f"forecast probability {float(prob):.0%} below floor")

        # --- market regime
        risk_on = ctx.get("risk_on")
        parts["regime"] = WEIGHTS["regime"] * (1.0 if risk_on else 0.35)
        if cfg["regime_filter"] and risk_on is False:
            blocks.append("regime is risk-off")

        # --- trend / location
        trend = float(ctx.get("trend_score") or 0.0)
        parts["trend"] = WEIGHTS["trend"] * _clamp((trend + 1.0) / 2.0, 0.0, 1.0)
        rsi = ctx.get("rsi")
        if rsi is not None and float(rsi) > cfg["max_rsi"]:
            blocks.append(f"RSI {float(rsi):.0f} is overextended")

        # --- volatility band
        atr_pct = ctx.get("atr_pct")
        if atr_pct is None:
            parts["volatility"] = WEIGHTS["volatility"] * 0.5
        else:
            a = float(atr_pct)
            if a < cfg["min_atr_pct"]:
                blocks.append(f"too quiet (ATR {a:.2f}%)")
            elif a > cfg["max_atr_pct"]:
                blocks.append(f"too wild (ATR {a:.2f}%)")
            sweet = 1.0 - abs(a - 1.0) / 3.0
            parts["volatility"] = WEIGHTS["volatility"] * _clamp(sweet, 0.1, 1.0)

        # --- liquidity / cost
        spread = float(ctx.get("spread_bps") or 0.0)
        parts["liquidity"] = WEIGHTS["liquidity"] * _clamp(1.0 - spread / max(1.0, cfg["max_spread_bps"]), 0.0, 1.0)
        if spread > cfg["max_spread_bps"]:
            blocks.append(f"spread {spread:.1f} bps too wide")
        qv = ctx.get("quote_volume")
        if cfg["min_quote_volume"] and qv is not None and float(qv) < cfg["min_quote_volume"]:
            blocks.append("24h volume below floor")

        # --- track record
        strategy = ctx.get("strategy") or "?"
        s_wr, s_n = self.win_rate(self.by_strategy, strategy)
        c_wr, c_n = self.win_rate(self.by_symbol, symbol)
        blended = (s_wr * min(s_n, 20) + c_wr * min(c_n, 20) + 0.5 * 10) / (min(s_n, 20) + min(c_n, 20) + 10)
        parts["track_record"] = WEIGHTS["track_record"] * _clamp((blended - 0.30) / 0.40, 0.0, 1.0)
        if (
            cfg["adaptive_weights"]
            and s_n >= cfg["min_strategy_trades"]
            and s_wr < cfg["min_strategy_winrate"]
        ):
            blocks.append(f"{strategy} is running {s_wr:.0%} on {s_n} trades")

        # --- portfolio / pacing guards
        if self.trades_today >= cfg["max_trades_per_day"]:
            blocks.append(f"daily trade cap ({int(cfg['max_trades_per_day'])}) reached")
        if self.consecutive_losses >= cfg["max_consecutive_losses"]:
            blocks.append(f"{self.consecutive_losses} losses in a row — standing down")
        if cfg["loss_cooldown_min"] and self.last_loss_ts:
            wait = cfg["loss_cooldown_min"] * 60 - (time.time() - self.last_loss_ts)
            if wait > 0:
                blocks.append(f"cooling off for {wait / 60:.0f} more min")
        last = self.last_trade_ts.get((symbol or "").upper(), 0.0)
        if cfg["symbol_cooldown_min"] and last:
            wait = cfg["symbol_cooldown_min"] * 60 - (time.time() - last)
            if wait > 0:
                blocks.append(f"{symbol} traded recently — {wait / 60:.0f} min cooldown")
        if int(ctx.get("open_correlated") or 0) >= cfg["max_open_correlated"]:
            blocks.append("too many correlated positions open")
        hours = cfg["session_hours"]
        if hours:
            hour = int(time.gmtime().tm_hour)
            if hour not in hours:
                blocks.append(f"outside trading session (UTC hour {hour})")

        score = round(sum(parts.values()), 1)
        passed = not blocks and score >= cfg["min_quality"]
        if not blocks and not passed:
            blocks.append(f"quality {score:.0f} below {cfg['min_quality']:.0f}")

        size_mult = _clamp(
            cfg["quality_size_floor"]
            + (cfg["quality_size_ceiling"] - cfg["quality_size_floor"])
            * _clamp((score - cfg["min_quality"]) / max(1.0, 100 - cfg["min_quality"]), 0.0, 1.0),
            cfg["quality_size_floor"],
            cfg["quality_size_ceiling"],
        )
        return {
            "symbol": symbol,
            "strategy": strategy,
            "score": score,
            "passed": bool(passed),
            "blocks": blocks,
            "notes": notes,
            "parts": {k: round(v, 2) for k, v in parts.items()},
            "size_mult": round(size_mult, 3),
            "strategy_win_rate": round(s_wr, 3),
            "strategy_trades": s_n,
            "symbol_win_rate": round(c_wr, 3),
            "symbol_trades": c_n,
        }

    def reject(self, decision: dict[str, Any]) -> None:
        entry = {
            "ts": time.time(),
            "symbol": decision.get("symbol"),
            "strategy": decision.get("strategy"),
            "score": decision.get("score"),
            "blocks": decision.get("blocks", []),
        }
        self.rejections.appendleft(entry)
        for reason in decision.get("blocks", []):
            key = reason.split("(")[0].strip()
            self.reject_counts[key] = self.reject_counts.get(key, 0) + 1

    # ------------------------------------------------------------- sizing
    def position_size(
        self,
        equity: float,
        price: float,
        atr_value: float,
        decision: dict[str, Any],
        max_notional: float,
        wins: int = 0,
        losses: int = 0,
    ) -> dict[str, Any]:
        """Volatility-targeted notional, scaled by quality and capped Kelly."""
        cfg = self.cfg
        risk_budget = equity * (cfg["vol_target_pct"] / 100.0)
        stop_distance = max(atr_value * cfg["atr_stop_mult"], price * 0.004)
        base = (risk_budget / stop_distance) * price if stop_distance > 0 else max_notional
        quality_mult = float(decision.get("size_mult") or 1.0)

        n = wins + losses
        kelly = 1.0
        if n >= 10:
            wr = wins / n
            edge = (2 * wr) - 1.0
            kelly = _clamp(0.5 + edge, 0.25, 1.0 + cfg["kelly_cap"])
        notional = base * quality_mult * kelly
        capped = min(notional, max_notional)
        return {
            "notional": round(capped, 2),
            "uncapped": round(notional, 2),
            "risk_budget": round(risk_budget, 2),
            "stop_distance": round(stop_distance, 8),
            "quality_mult": round(quality_mult, 3),
            "kelly_mult": round(kelly, 3),
            "capped": capped < notional,
        }

    def initial_levels(self, price: float, atr_value: float, fallback_stop: float, fallback_take: float) -> dict[str, float]:
        cfg = self.cfg
        if atr_value > 0:
            stop = price - cfg["atr_stop_mult"] * atr_value
            take = price + cfg["atr_take_mult"] * atr_value
        else:
            stop, take = fallback_stop, fallback_take
        stop = min(stop, price * 0.999)
        take = max(take, price * 1.001)
        return {"stop": stop, "take": take, "risk": max(price - stop, price * 0.0005)}

    # -------------------------------------------------------------- exits
    def manage(self, pos: Any, price: float, now: float | None = None) -> list[dict[str, Any]]:
        """Return the actions to apply to an open long position."""
        cfg = self.cfg
        now = now or time.time()
        actions: list[dict[str, Any]] = []
        entry = float(pos.entry)
        if entry <= 0 or price <= 0:
            return actions
        risk = max(entry - float(pos.stop or 0), entry * 0.0005)
        r = (price - entry) / risk
        peak_r = (float(pos.peak or entry) - entry) / risk
        held = now - float(pos.opened_ts or now)

        # Protective exits win over anything else: if the trade is being closed
        # there is no point moving a stop or scaling out of it first.
        # 1. giveback lock — do not let a winner become a loser
        if cfg["giveback_pct"] and peak_r >= 1.0 and r <= peak_r * (1 - cfg["giveback_pct"]) and held > cfg["min_hold_sec"]:
            return [{
                "kind": "close",
                "reason": f"gave back {cfg['giveback_pct']:.0%} of a {peak_r:.1f}R move",
            }]

        # 2. time stop for trades that go nowhere
        if cfg["time_stop_min"] and held > cfg["time_stop_min"] * 60 and r < 0.5:
            return [{"kind": "close", "reason": f"time stop after {held / 60:.0f} min"}]

        # 3. break-even once the trade has paid for itself
        if cfg["breakeven_at_r"] and r >= cfg["breakeven_at_r"] and pos.stop < entry:
            actions.append({"kind": "stop", "value": entry, "reason": f"break-even at {r:.1f}R"})

        # 4. partial ladder
        taken = int(getattr(pos, "partials_taken", 0) or 0)
        if cfg["partial_1_frac"] and taken < 1 and r >= cfg["partial_1_r"]:
            actions.append({
                "kind": "scale", "frac": cfg["partial_1_frac"],
                "reason": f"partial {cfg['partial_1_frac']:.0%} at {cfg['partial_1_r']:.1f}R",
            })
        elif cfg["partial_2_frac"] and taken == 1 and r >= cfg["partial_2_r"]:
            actions.append({
                "kind": "scale", "frac": cfg["partial_2_frac"],
                "reason": f"partial {cfg['partial_2_frac']:.0%} at {cfg['partial_2_r']:.1f}R",
            })

        # 5. ATR trailing stop once in profit
        if pos.atr and r >= 1.0:
            trail = float(pos.peak or price) - cfg["atr_trail_mult"] * float(pos.atr)
            if trail > pos.stop:
                actions.append({"kind": "stop", "value": trail, "reason": "ATR trail"})

        return actions

    # --------------------------------------------------------------- views
    def stats(self) -> dict[str, Any]:
        strategies = [
            {
                "name": k,
                "trades": int(v.get("n") or 0),
                "win_rate": round((v.get("wins") or 0) / max(1, v.get("n") or 1), 3),
                "pnl": round(v.get("pnl") or 0.0, 2),
                "avg_r": round((v.get("r") or 0.0) / max(1, v.get("n") or 1), 3),
            }
            for k, v in self.by_strategy.items()
        ]
        strategies.sort(key=lambda r: -r["pnl"])
        symbols = [
            {
                "symbol": k,
                "trades": int(v.get("n") or 0),
                "win_rate": round((v.get("wins") or 0) / max(1, v.get("n") or 1), 3),
                "pnl": round(v.get("pnl") or 0.0, 2),
            }
            for k, v in self.by_symbol.items()
        ]
        symbols.sort(key=lambda r: -r["pnl"])
        top_blocks = sorted(self.reject_counts.items(), key=lambda kv: -kv[1])[:10]
        return {
            "cfg": dict(self.cfg),
            "defaults": dict(DEFAULTS),
            "accepted": self.accepted,
            "rejected": sum(self.reject_counts.values()),
            "trades_today": self.trades_today,
            "consecutive_losses": self.consecutive_losses,
            "by_strategy": strategies,
            "by_symbol": symbols[:25],
            "top_blocks": [{"reason": k, "count": v} for k, v in top_blocks],
            "recent_rejections": list(self.rejections)[:25],
        }
