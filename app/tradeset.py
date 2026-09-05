"""Which coins the bot is allowed to trade.

Scanning and trading are deliberately separate concerns: the watchlist is what
the terminal *analyses*, the trade set is what it is allowed to *buy*. You can
watch 40 coins and trade one.

Three modes:

* ``selected`` — trade exactly the coins you ticked (default),
* ``all``      — trade anything on the watchlist,
* ``auto``     — trade the top N coins by live screener score, refreshed as the
  board moves, optionally pinned to a manual core.

Per-coin overrides let you cap size or arm/disarm a single coin without
retyping the whole list.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("tradeset")

MODES = ("selected", "all", "auto")
AUTO_METRICS = ("score", "alpha", "momentum", "volume")


def _norm(symbol: str) -> str:
    return (symbol or "").upper().replace("-", "/").strip()


class TradeSet:
    """Persisted allow-list of tradable symbols."""

    def __init__(self, path: Path):
        self.path = path
        self.mode: str = "selected"
        self.symbols: list[str] = []
        self.auto_top_n: int = 5
        self.auto_metric: str = "score"
        self.auto_min_volume: float = 0.0
        self.pinned: list[str] = []
        self.per_symbol: dict[str, dict[str, Any]] = {}
        self.updated: float = 0.0
        self._auto_cache: list[str] = []
        self.load()

    # ------------------------------------------------------------------ io
    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except Exception:
            return
        self.mode = raw.get("mode") if raw.get("mode") in MODES else "selected"
        self.symbols = [_norm(s) for s in raw.get("symbols") or []]
        self.pinned = [_norm(s) for s in raw.get("pinned") or []]
        self.auto_top_n = max(1, min(25, int(raw.get("auto_top_n") or 5)))
        metric = raw.get("auto_metric")
        self.auto_metric = metric if metric in AUTO_METRICS else "score"
        self.auto_min_volume = float(raw.get("auto_min_volume") or 0.0)
        self.per_symbol = {
            _norm(k): dict(v) for k, v in (raw.get("per_symbol") or {}).items() if isinstance(v, dict)
        }
        self.updated = float(raw.get("updated") or 0.0)

    def save(self) -> None:
        self.updated = time.time()
        payload = {
            "mode": self.mode,
            "symbols": self.symbols,
            "pinned": self.pinned,
            "auto_top_n": self.auto_top_n,
            "auto_metric": self.auto_metric,
            "auto_min_volume": self.auto_min_volume,
            "per_symbol": self.per_symbol,
            "updated": self.updated,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), "utf-8")
            os.replace(tmp, self.path)
        except Exception as exc:  # pragma: no cover - disk only
            log.warning("could not persist trade set: %s", exc)

    # -------------------------------------------------------------- config
    def configure(self, patch: dict[str, Any]) -> dict[str, Any]:
        if "mode" in patch and patch["mode"] in MODES:
            self.mode = patch["mode"]
        if "symbols" in patch:
            self.symbols = list(dict.fromkeys(_norm(s) for s in patch["symbols"] or [] if s))
        if "pinned" in patch:
            self.pinned = list(dict.fromkeys(_norm(s) for s in patch["pinned"] or [] if s))
        if "auto_top_n" in patch:
            self.auto_top_n = max(1, min(25, int(patch["auto_top_n"] or 5)))
        if "auto_metric" in patch and patch["auto_metric"] in AUTO_METRICS:
            self.auto_metric = patch["auto_metric"]
        if "auto_min_volume" in patch:
            self.auto_min_volume = max(0.0, float(patch["auto_min_volume"] or 0.0))
        if "per_symbol" in patch and isinstance(patch["per_symbol"], dict):
            for sym, cfg in patch["per_symbol"].items():
                if isinstance(cfg, dict):
                    self.per_symbol[_norm(sym)] = cfg
        self.save()
        return self.to_dict()

    def select(self, symbols: Iterable[str], mode: str = "selected") -> dict[str, Any]:
        return self.configure({"symbols": list(symbols), "mode": mode})

    def toggle(self, symbol: str, on: bool | None = None) -> bool:
        sym = _norm(symbol)
        has = sym in self.symbols
        want = (not has) if on is None else bool(on)
        if want and not has:
            self.symbols.append(sym)
        elif not want and has:
            self.symbols = [s for s in self.symbols if s != sym]
        self.save()
        return want

    def set_enabled(self, symbol: str, enabled: bool) -> None:
        sym = _norm(symbol)
        cfg = self.per_symbol.setdefault(sym, {})
        cfg["enabled"] = bool(enabled)
        self.save()

    def size_multiplier(self, symbol: str) -> float:
        cfg = self.per_symbol.get(_norm(symbol)) or {}
        try:
            return max(0.1, min(3.0, float(cfg.get("size_mult", 1.0))))
        except (TypeError, ValueError):
            return 1.0

    # -------------------------------------------------------------- runtime
    def refresh_auto(self, rows: list[dict[str, Any]]) -> list[str]:
        """Recompute the auto basket from the latest screener rows."""
        if self.mode != "auto":
            return self._auto_cache
        # screener column names differ from the friendly metric labels
        metric = {
            "score": "alpha",
            "alpha": "alpha",
            "momentum": "mom_score",
            "volume": "volume",
        }.get(self.auto_metric, "alpha")
        ranked = []
        for r in rows or []:
            sym = _norm(r.get("symbol", ""))
            if not sym:
                continue
            vol = float(r.get("quote_volume") or 0) or float(r.get("volume") or 0) * float(r.get("last") or 1)
            if self.auto_min_volume and vol < self.auto_min_volume:
                continue
            value = r.get(metric)
            if value is None:
                value = r.get("alpha", r.get("score", 0))
            try:
                ranked.append((float(value), sym))
            except (TypeError, ValueError):
                continue
        ranked.sort(reverse=True)
        picked = [s for _, s in ranked[: self.auto_top_n]]
        self._auto_cache = list(dict.fromkeys([*self.pinned, *picked]))
        return self._auto_cache

    def active(self, watchlist: Iterable[str]) -> list[str]:
        """Symbols the bot may open new positions in, right now."""
        watch = [_norm(s) for s in watchlist]
        if self.mode == "all":
            chosen = watch
        elif self.mode == "auto":
            chosen = self._auto_cache or self.pinned or watch[:1]
        else:
            chosen = [s for s in self.symbols if s in watch] or []
        out = []
        for sym in chosen:
            cfg = self.per_symbol.get(sym) or {}
            if cfg.get("enabled") is False:
                continue
            out.append(sym)
        return list(dict.fromkeys(out))

    def allows(self, symbol: str, watchlist: Iterable[str]) -> tuple[bool, str]:
        sym = _norm(symbol)
        cfg = self.per_symbol.get(sym) or {}
        if cfg.get("enabled") is False:
            return False, "coin disarmed"
        if sym in self.active(watchlist):
            return True, "ok"
        if self.mode == "auto":
            return False, "not in the auto basket right now"
        return False, "coin not selected for trading"

    # ---------------------------------------------------------------- views
    def to_dict(self, watchlist: Iterable[str] | None = None) -> dict[str, Any]:
        watch = [_norm(s) for s in (watchlist or [])]
        return {
            "mode": self.mode,
            "symbols": self.symbols,
            "pinned": self.pinned,
            "auto_top_n": self.auto_top_n,
            "auto_metric": self.auto_metric,
            "auto_min_volume": self.auto_min_volume,
            "per_symbol": self.per_symbol,
            "active": self.active(watch) if watch else self.active(self.symbols),
            "auto_basket": self._auto_cache,
            "modes": list(MODES),
            "metrics": list(AUTO_METRICS),
            "updated": self.updated,
        }
