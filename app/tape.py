"""Cross-exchange trade tape.

The market hub keeps a 400-tick global ring buffer, which is enough to print a
scrolling tape and nothing else. Order flow needs more: every print for a symbol,
kept long enough to build bars, split by aggressor side, and attributed to the
venue it happened on.

This module records that. It buckets prints into fixed 5-second cells as they
arrive, so memory stays flat no matter how busy the tape gets, and any chart
timeframe is a resample of those cells.

Nothing here is exchange-specific: it consumes the normalised ``TradeTick`` the
feeds already emit.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Iterable

CELL_SECONDS = 5.0
#: how many cells to keep per symbol — 4320 cells = 6 hours of tape
MAX_CELLS = 4320
#: individual prints worth remembering per symbol
MAX_PRINTS = 400
#: price levels are binned so a footprint does not explode into thousands of rows
LEVEL_BINS = 48


@dataclass
class Cell:
    """One 5-second slice of the consolidated tape."""

    ts: float
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    buy_vol: float = 0.0
    sell_vol: float = 0.0
    buy_trades: int = 0
    sell_trades: int = 0
    notional: float = 0.0
    venues: dict[str, list[float]] = field(default_factory=dict)  # venue -> [buy, sell]
    levels: dict[float, list[float]] = field(default_factory=dict)  # price -> [buy, sell]

    @property
    def volume(self) -> float:
        return self.buy_vol + self.sell_vol

    @property
    def delta(self) -> float:
        return self.buy_vol - self.sell_vol

    def add(self, price: float, qty: float, side: str, venue: str) -> None:
        if not self.open:
            self.open = self.high = self.low = price
        self.high = max(self.high, price)
        self.low = min(self.low, price) if self.low else price
        self.close = price
        self.notional += price * qty
        buy = side == "buy"
        if buy:
            self.buy_vol += qty
            self.buy_trades += 1
        else:
            self.sell_vol += qty
            self.sell_trades += 1
        slot = self.venues.setdefault(venue, [0.0, 0.0])
        slot[0 if buy else 1] += qty
        level = self.levels.setdefault(price, [0.0, 0.0])
        level[0 if buy else 1] += qty


class TapeBook:
    """Per-symbol consolidated tape, aggregated as it arrives."""

    def __init__(self, cell_seconds: float = CELL_SECONDS, max_cells: int = MAX_CELLS):
        self.cell_seconds = cell_seconds
        self.max_cells = max_cells
        self.cells: dict[str, deque[Cell]] = defaultdict(lambda: deque(maxlen=max_cells))
        self.prints: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=MAX_PRINTS))
        self.venues: dict[str, set[str]] = defaultdict(set)
        self.counts: dict[str, int] = defaultdict(int)
        self._avg_qty: dict[str, float] = defaultdict(float)

    # ------------------------------------------------------------- recording
    def record(self, symbol: str, price: float, qty: float, side: str, venue: str, ts: float | None = None) -> None:
        if not symbol or price <= 0 or qty <= 0:
            return
        ts = time.time() if ts is None else float(ts)
        side = "buy" if str(side).lower().startswith("b") else "sell"
        bucket = ts - (ts % self.cell_seconds)
        cells = self.cells[symbol]
        if not cells or cells[-1].ts != bucket:
            # a late print for an older cell still lands in the right place
            if cells and bucket < cells[-1].ts:
                for cell in reversed(cells):
                    if cell.ts == bucket:
                        cell.add(self._bin(symbol, price), qty, side, venue)
                        return
                return
            cells.append(Cell(ts=bucket))
        cells[-1].add(self._bin(symbol, price), qty, side, venue)

        self.venues[symbol].add(venue)
        self.counts[symbol] += 1
        # a slow exponential average of print size, used to spot the big ones
        n = self._avg_qty[symbol]
        self._avg_qty[symbol] = qty if not n else n * 0.995 + qty * 0.005
        if qty >= self._avg_qty[symbol] * 4:
            self.prints[symbol].appendleft(
                {
                    "ts": ts,
                    "price": price,
                    "qty": qty,
                    "notional": price * qty,
                    "side": side,
                    "venue": venue,
                    "ratio": round(qty / max(self._avg_qty[symbol], 1e-12), 1),
                }
            )

    def _bin(self, symbol: str, price: float) -> float:
        """Round a price onto a grid so footprint levels stay countable."""
        cells = self.cells.get(symbol)
        ref = 0.0
        if cells:
            ref = cells[-1].close or cells[-1].open
        ref = ref or price
        # a bin roughly 5 bps wide, snapped to a readable step
        step = max(ref * 5e-4, 1e-9)
        mag = 10 ** (len(str(int(step))) - 1) if step >= 1 else 10 ** -(len(f"{step:.10f}".split(".")[1].lstrip("0")) or 1)
        step = max(round(step / mag) * mag, mag)
        return round(round(price / step) * step, 10)

    # -------------------------------------------------------------- reading
    def bars(self, symbol: str, tf_seconds: float, limit: int = 200) -> list[dict[str, Any]]:
        """Resample the cells of one symbol into bars of ``tf_seconds``."""
        cells = self.cells.get(symbol)
        if not cells:
            return []
        buckets: dict[float, Cell] = {}
        order: list[float] = []
        for cell in cells:
            key = cell.ts - (cell.ts % tf_seconds)
            merged = buckets.get(key)
            if merged is None:
                merged = Cell(ts=key, open=cell.open, high=cell.high, low=cell.low or cell.open)
                buckets[key] = merged
                order.append(key)
            merged.high = max(merged.high, cell.high)
            merged.low = min(merged.low, cell.low) if merged.low and cell.low else (merged.low or cell.low)
            merged.close = cell.close
            merged.buy_vol += cell.buy_vol
            merged.sell_vol += cell.sell_vol
            merged.buy_trades += cell.buy_trades
            merged.sell_trades += cell.sell_trades
            merged.notional += cell.notional
            for venue, (b, s) in cell.venues.items():
                slot = merged.venues.setdefault(venue, [0.0, 0.0])
                slot[0] += b
                slot[1] += s
            for price, (b, s) in cell.levels.items():
                slot = merged.levels.setdefault(price, [0.0, 0.0])
                slot[0] += b
                slot[1] += s
        out = [self._bar(buckets[k]) for k in order[-limit:]]
        return out

    def _bar(self, cell: Cell) -> dict[str, Any]:
        vol = cell.volume
        poc = max(cell.levels.items(), key=lambda kv: kv[1][0] + kv[1][1])[0] if cell.levels else cell.close
        return {
            "ts": cell.ts,
            "open": cell.open,
            "high": cell.high,
            "low": cell.low or cell.open,
            "close": cell.close,
            "volume": vol,
            "buy_vol": cell.buy_vol,
            "sell_vol": cell.sell_vol,
            "delta": cell.delta,
            "delta_pct": (cell.delta / vol) if vol else 0.0,
            "trades": cell.buy_trades + cell.sell_trades,
            "vwap": (cell.notional / vol) if vol else cell.close,
            "poc": poc,
            "venues": {v: {"buy": round(b, 8), "sell": round(s, 8)} for v, (b, s) in cell.venues.items()},
            "levels": sorted(
                ({"price": p, "buy": b, "sell": s} for p, (b, s) in cell.levels.items()),
                key=lambda r: -r["price"],
            )[:LEVEL_BINS],
            "estimated": False,
        }

    def big_prints(self, symbol: str, limit: int = 40) -> list[dict[str, Any]]:
        return list(self.prints.get(symbol, []))[:limit]

    def coverage(self, symbol: str) -> dict[str, Any]:
        cells = self.cells.get(symbol) or []
        return {
            "symbol": symbol,
            "cells": len(cells),
            "prints": self.counts.get(symbol, 0),
            "venues": sorted(self.venues.get(symbol, set())),
            "seconds": round((cells[-1].ts - cells[0].ts) if len(cells) > 1 else 0.0, 1),
            "avg_print": round(self._avg_qty.get(symbol, 0.0), 8),
        }

    def symbols(self) -> list[str]:
        return sorted(self.cells)

    def stats(self) -> dict[str, Any]:
        return {
            "symbols": len(self.cells),
            "cells": sum(len(c) for c in self.cells.values()),
            "prints": sum(self.counts.values()),
            "venues": sorted({v for s in self.venues.values() for v in s}),
            "cell_seconds": self.cell_seconds,
            "window_hours": round(self.max_cells * self.cell_seconds / 3600, 1),
        }


# --------------------------------------------------------------------------- #
# candle-shape fallback
# --------------------------------------------------------------------------- #


def estimate_bars(candles: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split candle volume into buy and sell without tick data.

    Where the close sits inside the bar's range is a decent proxy for who won
    the bar: a close on the high means buyers absorbed everything offered. This
    is the standard estimator used when a tape is not available, and every bar
    it produces is flagged ``estimated`` so the UI can say so.
    """
    out: list[dict[str, Any]] = []
    for c in candles:
        high = float(c.get("high") or 0)
        low = float(c.get("low") or 0)
        close = float(c.get("close") or 0)
        vol = float(c.get("volume") or 0)
        span = high - low
        ratio = 0.5 if span <= 0 else max(0.0, min(1.0, (close - low) / span))
        buy = vol * ratio
        sell = vol - buy
        out.append(
            {
                "ts": float(c.get("ts") or 0),
                "open": float(c.get("open") or close),
                "high": high or close,
                "low": low or close,
                "close": close,
                "volume": vol,
                "buy_vol": buy,
                "sell_vol": sell,
                "delta": buy - sell,
                "delta_pct": (buy - sell) / vol if vol else 0.0,
                "trades": 0,
                "vwap": (high + low + close) / 3 if span else close,
                "poc": close,
                "venues": {},
                "levels": [],
                "estimated": True,
            }
        )
    return out
