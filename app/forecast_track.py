"""Forecast scoreboard.

Every prediction the robot makes is recorded with its horizon, then graded
against the real price once that horizon elapses. That turns the forecast
ensemble from a black box into something measurable: hit rate, Brier score,
band coverage, per-model accuracy and a calibration curve.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from app.timeframes import TF_SECONDS

log = logging.getLogger("forecast_track")

STORE_PATH = Path("data/forecast_log.json")

MAX_OPEN = 400
MAX_SETTLED = 1000
# a call only counts as directional if it expects at least this much movement
DEAD_ZONE_PCT = 0.02

CALIBRATION_BUCKETS = [
    (0.0, 40.0, "<40%"),
    (40.0, 50.0, "40-50%"),
    (50.0, 60.0, "50-60%"),
    (60.0, 70.0, "60-70%"),
    (70.0, 101.0, "70%+"),
]


def horizon_seconds(timeframe: str, bars: int) -> float:
    return float(TF_SECONDS.get(timeframe, 60) * max(1, int(bars)))


def _round(v: Any, d: int = 3) -> Any:
    try:
        return round(float(v), d)
    except (TypeError, ValueError):
        return v


class ForecastTracker:
    """Records forecasts, settles them at maturity and scores the ensemble."""

    def __init__(self, path: Path | str = STORE_PATH, min_gap_sec: float = 120.0):
        self.path = Path(path)
        self.min_gap_sec = float(min_gap_sec)
        self.open: list[dict[str, Any]] = []
        self.settled: list[dict[str, Any]] = []
        self._dirty = 0
        self.load()

    # -- persistence ------------------------------------------------------ #
    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except Exception:
            return
        self.open = list(raw.get("open") or [])[:MAX_OPEN]
        self.settled = list(raw.get("settled") or [])[:MAX_SETTLED]

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"open": self.open[:MAX_OPEN], "settled": self.settled[:MAX_SETTLED]})
            )
            tmp.replace(self.path)
            self._dirty = 0
        except Exception as exc:  # pragma: no cover - disk issues only
            log.debug("forecast log save failed: %s", exc)

    # -- recording -------------------------------------------------------- #
    def record(self, forecast: dict[str, Any], now: float | None = None) -> bool:
        """Log a forecast for later grading. Returns True when it was stored."""
        if not forecast or not forecast.get("ok"):
            return False
        symbol = forecast.get("symbol")
        tf = forecast.get("timeframe", "1m")
        price = float(forecast.get("price") or 0.0)
        if not symbol or price <= 0:
            return False
        now = now if now is not None else time.time()
        # don't spam the log with the same call every loop
        for row in self.open:
            if row["symbol"] == symbol and row["timeframe"] == tf and now - row["ts"] < self.min_gap_sec:
                return False
        entry = self._entry(forecast, now)
        self.open.insert(0, entry)
        del self.open[MAX_OPEN:]
        self._dirty += 1
        return True

    def _entry(self, forecast: dict[str, Any], now: float, source: str = "live") -> dict[str, Any]:
        symbol = forecast.get("symbol")
        tf = forecast.get("timeframe", "1m")
        price = float(forecast.get("price") or 0.0)
        bars = int(forecast.get("horizon_bars") or 1)
        return {
            "symbol": symbol,
            "timeframe": tf,
            "ts": now,
            "due_ts": now + horizon_seconds(tf, bars),
            "horizon_bars": bars,
            "horizon_label": forecast.get("horizon_label", ""),
            "price": price,
            "direction": forecast.get("direction", "flat"),
            "probability_up": float(forecast.get("probability_up") or 50.0),
            "expected_move_pct": float(forecast.get("expected_move_pct") or 0.0),
            "confidence": float(forecast.get("confidence") or 0.0),
            "target": forecast.get("target"),
            "upper": forecast.get("upper"),
            "lower": forecast.get("lower"),
            "regime": (forecast.get("regime") or {}).get("name", "unknown"),
            "source": source,
            "models": [
                {"name": m.get("name"), "score": _round(m.get("score"), 3)}
                for m in (forecast.get("models") or [])
            ],
        }

    def grade_historic(
        self,
        forecast: dict[str, Any],
        exit_price: float,
        ts: float,
        source: str = "backfill",
    ) -> dict[str, Any] | None:
        """Score a forecast made at a past bar against what actually happened.

        Used to seed the scoreboard from candle history so the panel is useful
        immediately instead of after the first horizon elapses. The caller must
        only pass forecasts built from data available at ``ts``.
        """
        if not forecast or not forecast.get("ok") or not exit_price:
            return None
        entry = self._entry(forecast, ts, source=source)
        if entry["price"] <= 0:
            return None
        row = self._grade(entry, float(exit_price), entry["due_ts"])
        self.settled.append(row)
        self.settled.sort(key=lambda r: -r.get("settled_ts", 0))
        del self.settled[MAX_SETTLED:]
        self._dirty += 1
        return row

    # -- settlement ------------------------------------------------------- #
    def settle(self, price_of: Callable[[str], float | None], now: float | None = None) -> list[dict[str, Any]]:
        """Grade every forecast whose horizon has elapsed."""
        now = now if now is not None else time.time()
        matured = [r for r in self.open if r["due_ts"] <= now]
        if not matured:
            return []
        done: list[dict[str, Any]] = []
        for row in matured:
            price = price_of(row["symbol"])
            if not price or price <= 0:
                # give up on stale rows we can no longer price
                if now - row["due_ts"] > 3600:
                    self.open.remove(row)
                continue
            self.open.remove(row)
            done.append(self._grade(row, float(price), now))
        if done:
            self.settled = done + self.settled
            del self.settled[MAX_SETTLED:]
            self._dirty += len(done)
        if self._dirty >= 5:
            self.save()
        return done

    def _grade(self, row: dict[str, Any], price: float, now: float) -> dict[str, Any]:
        actual = (price / row["price"] - 1.0) * 100.0
        expected = row["expected_move_pct"]
        directional = row["direction"] in ("up", "down") and abs(expected) >= DEAD_ZONE_PCT
        up = actual > 0
        hit = None
        if directional:
            hit = (row["direction"] == "up") == up
        prob = row["probability_up"] / 100.0
        brier = (prob - (1.0 if up else 0.0)) ** 2
        lower, upper = row.get("lower"), row.get("upper")
        in_band = None
        if lower and upper:
            in_band = bool(float(lower) <= price <= float(upper))
        models = []
        for m in row.get("models") or []:
            score = float(m.get("score") or 0.0)
            models.append(
                {
                    "name": m.get("name"),
                    "score": score,
                    "hit": None if abs(score) < 0.03 else ((score > 0) == up),
                }
            )
        return {
            **row,
            "settled_ts": now,
            "exit_price": price,
            "actual_move_pct": round(actual, 4),
            "error_pct": round(abs(actual - expected), 4),
            "hit": hit,
            "brier": round(brier, 4),
            "in_band": in_band,
            "models": models,
        }

    # -- scoring ---------------------------------------------------------- #
    def stats(self, limit: int = 400) -> dict[str, Any]:
        rows = self.settled[:limit]
        graded = [r for r in rows if r.get("hit") is not None]
        out: dict[str, Any] = {
            "open": len(self.open),
            "settled": len(rows),
            "graded": len(graded),
            "hit_rate": None,
            "brier": None,
            "mae_pct": None,
            "band_coverage": None,
            "avg_confidence": None,
            "edge": None,
            "by_timeframe": [],
            "by_model": [],
            "calibration": [],
            "recent": [
                {
                    k: r.get(k)
                    for k in (
                        "symbol",
                        "timeframe",
                        "direction",
                        "probability_up",
                        "expected_move_pct",
                        "actual_move_pct",
                        "hit",
                        "in_band",
                        "confidence",
                        "settled_ts",
                        "horizon_label",
                    )
                }
                for r in rows[:20]
            ],
            "pending": [
                {
                    "symbol": r["symbol"],
                    "timeframe": r["timeframe"],
                    "direction": r["direction"],
                    "probability_up": r["probability_up"],
                    "due_in": max(0.0, round(r["due_ts"] - time.time(), 1)),
                    "horizon_label": r.get("horizon_label", ""),
                }
                for r in self.open[:12]
            ],
        }
        if not rows:
            return out
        out["live"] = sum(1 for r in rows if r.get("source", "live") == "live")
        out["backfilled"] = sum(1 for r in rows if r.get("source") == "backfill")
        out["brier"] = round(sum(r["brier"] for r in rows) / len(rows), 4)
        out["mae_pct"] = round(sum(r["error_pct"] for r in rows) / len(rows), 4)
        out["avg_confidence"] = round(sum(r["confidence"] for r in rows) / len(rows), 1)
        banded = [r for r in rows if r.get("in_band") is not None]
        if banded:
            out["band_coverage"] = round(sum(1 for r in banded if r["in_band"]) / len(banded) * 100, 1)
        if graded:
            out["hit_rate"] = round(sum(1 for r in graded if r["hit"]) / len(graded) * 100, 1)
            out["edge"] = round(out["hit_rate"] - 50.0, 1)

        # per timeframe
        frames: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            frames.setdefault(r["timeframe"], []).append(r)
        for tf, group in frames.items():
            g = [r for r in group if r.get("hit") is not None]
            out["by_timeframe"].append(
                {
                    "timeframe": tf,
                    "n": len(group),
                    "hit_rate": round(sum(1 for r in g if r["hit"]) / len(g) * 100, 1) if g else None,
                    "brier": round(sum(r["brier"] for r in group) / len(group), 4),
                    "mae_pct": round(sum(r["error_pct"] for r in group) / len(group), 4),
                }
            )
        out["by_timeframe"].sort(key=lambda r: -r["n"])

        # per model
        tally: dict[str, list[int]] = {}
        for r in rows:
            for m in r.get("models") or []:
                if m.get("hit") is None:
                    continue
                slot = tally.setdefault(m["name"], [0, 0])
                slot[0] += 1
                slot[1] += 1 if m["hit"] else 0
        for name, (n, wins) in tally.items():
            out["by_model"].append(
                {"name": name, "n": n, "hit_rate": round(wins / n * 100, 1), "edge": round(wins / n * 100 - 50, 1)}
            )
        out["by_model"].sort(key=lambda r: -r["hit_rate"])

        # calibration — did 70% calls actually happen 70% of the time?
        for lo, hi, label in CALIBRATION_BUCKETS:
            bucket = [r for r in rows if lo <= r["probability_up"] < hi]
            if not bucket:
                continue
            realized = sum(1 for r in bucket if r["actual_move_pct"] > 0) / len(bucket) * 100
            predicted = sum(r["probability_up"] for r in bucket) / len(bucket)
            out["calibration"].append(
                {
                    "bucket": label,
                    "n": len(bucket),
                    "predicted": round(predicted, 1),
                    "realized": round(realized, 1),
                    "gap": round(realized - predicted, 1),
                }
            )
        return out

    def record_many(self, forecasts: Iterable[dict[str, Any]], now: float | None = None) -> int:
        return sum(1 for f in forecasts if self.record(f, now=now))
