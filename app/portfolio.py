"""Portfolio risk: what you are actually exposed to, and what it can cost.

Position-level PnL is the easy part. This module answers the questions that
decide whether a book survives a bad week:

* how concentrated am I, and in what,
* how much do these positions actually move together,
* what do I lose if every stop hits at once (open risk),
* what does a bad day look like historically (VaR / expected shortfall),
* is my edge coming from a few outliers (R-multiple distribution),
* which setups — by tag — actually pay.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("portfolio")


def _returns(closes: list[float]) -> list[float]:
    out = []
    for a, b in zip(closes, closes[1:]):
        if a > 0 and b > 0:
            out.append(b / a - 1.0)
    return out


def _corr(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 8:
        return 0.0
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 0 or vb <= 0:
        return 0.0
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    return max(-1.0, min(1.0, cov / math.sqrt(va * vb)))


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return ordered[int(k)]
    return ordered[lo] * (hi - k) + ordered[hi] * (k - lo)


class Journal:
    """Tags and notes on closed trades, persisted next to the fills."""

    def __init__(self, path: Path):
        self.path = path
        self.entries: dict[str, dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            if isinstance(raw, dict):
                self.entries = raw
        except Exception:
            pass

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.entries, indent=2), "utf-8")
            os.replace(tmp, self.path)
        except Exception as exc:  # pragma: no cover - disk only
            log.warning("could not persist journal: %s", exc)

    def annotate(self, fill_id: str, note: str | None = None, tags: list[str] | None = None,
                 rating: int | None = None) -> dict[str, Any]:
        entry = self.entries.setdefault(fill_id, {"tags": [], "note": "", "rating": 0})
        if note is not None:
            entry["note"] = note[:2000]
        if tags is not None:
            entry["tags"] = sorted({t.strip().lower()[:24] for t in tags if t.strip()})
        if rating is not None:
            entry["rating"] = max(0, min(5, int(rating)))
        entry["updated"] = time.time()
        self.save()
        return entry

    def get(self, fill_id: str) -> dict[str, Any]:
        return self.entries.get(fill_id) or {"tags": [], "note": "", "rating": 0}

    def by_tag(self, fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets: dict[str, list[float]] = {}
        for f in fills:
            entry = self.entries.get(f.get("id") or "")
            if not entry:
                continue
            for tag in entry.get("tags") or []:
                buckets.setdefault(tag, []).append(float(f.get("pnl") or 0.0))
        rows = []
        for tag, pnls in buckets.items():
            wins = [p for p in pnls if p > 0]
            rows.append(
                {
                    "tag": tag,
                    "trades": len(pnls),
                    "net": round(sum(pnls), 2),
                    "win_rate": round(len(wins) / len(pnls), 3) if pnls else 0.0,
                    "avg": round(sum(pnls) / len(pnls), 2) if pnls else 0.0,
                }
            )
        rows.sort(key=lambda r: -r["net"])
        return rows

    def tags(self) -> list[str]:
        out: set[str] = set()
        for entry in self.entries.values():
            out.update(entry.get("tags") or [])
        return sorted(out)


def exposure(positions: list[dict[str, Any]], equity: float, cash: float) -> dict[str, Any]:
    """Where the money is, and how lopsided that is."""
    rows = []
    gross = 0.0
    net = 0.0
    for p in positions:
        value = float(p.get("qty") or 0) * float(p.get("price") or p.get("entry") or 0)
        short = str(p.get("side") or "buy").lower() in ("sell", "short")
        gross += value
        net += -value if short else value
        rows.append(
            {
                "symbol": p.get("symbol"),
                "base": str(p.get("symbol") or "/").split("/")[0],
                "side": "short" if short else "long",
                "value": round(value, 2),
                "notional": round(value, 2),
                "pct_equity": round(value / equity * 100, 2) if equity else 0.0,
                "unrealized": round(float(p.get("unrealized") or 0), 2),
                "strategy": p.get("strategy"),
            }
        )
    rows.sort(key=lambda r: -r["value"])
    for r in rows:
        r["weight"] = round(r["value"] / gross, 4) if gross else 0.0
    weights = [r["weight"] for r in rows]
    hhi = sum(w * w for w in weights)
    return {
        "rows": rows,
        "gross": round(gross, 2),
        "net": round(net, 2),
        "gross_pct": round(gross / equity * 100, 2) if equity else 0.0,
        "net_pct": round(gross / equity * 100, 2) if equity else 0.0,
        "cash_pct": round(cash / equity * 100, 2) if equity else 0.0,
        "positions": len(rows),
        "largest_pct": rows[0]["pct_equity"] if rows else 0.0,
        "concentration": round(hhi, 3),
        "concentration_label": (
            "single-name risk" if hhi > 0.5 else "concentrated" if hhi > 0.3 else "diversified"
        ),
    }


def open_risk(positions: list[dict[str, Any]], equity: float) -> dict[str, Any]:
    """What it costs if every stop is hit at once."""
    rows = []
    total = 0.0
    for p in positions:
        qty = float(p.get("qty") or 0)
        price = float(p.get("price") or p.get("entry") or 0)
        stop = float(p.get("stop") or 0)
        if qty <= 0 or price <= 0:
            continue
        entry = float(p.get("entry") or price)
        risk = max(0.0, (price - stop) * qty) if stop else 0.0
        total += risk
        unit = entry - stop if stop and entry > stop else 0.0
        rows.append(
            {
                "symbol": p.get("symbol"),
                "risk": round(risk, 2),
                "pct_equity": round(risk / equity * 100, 3) if equity else 0.0,
                "stop": stop,
                "entry": entry,
                "price": price,
                # how much of the initial risk the trade is currently up
                "r_open": round((price - entry) / unit, 2) if unit else None,
                "distance_pct": round((price / stop - 1) * 100, 2) if stop else None,
                "protected": bool(stop),
            }
        )
    rows.sort(key=lambda r: -r["risk"])
    return {
        "rows": rows,
        "total": round(total, 2),
        "pct_equity": round(total / equity * 100, 3) if equity else 0.0,
        "unprotected": [r["symbol"] for r in rows if not r["protected"]],
    }


def correlations(series: dict[str, list[float]], limit: int = 12) -> dict[str, Any]:
    """Pairwise correlation of recent returns across the book."""
    rets = {sym: _returns(closes[-200:]) for sym, closes in series.items() if len(closes) > 20}
    symbols = list(rets)[:limit]
    matrix = [[round(_corr(rets[a], rets[b]), 3) for b in symbols] for a in symbols]
    pairs = []
    for i, a in enumerate(symbols):
        for j, b in enumerate(symbols):
            if j <= i:
                continue
            pairs.append({"a": a, "b": b, "corr": matrix[i][j]})
    pairs.sort(key=lambda r: -abs(r["corr"]))
    avg = round(sum(p["corr"] for p in pairs) / len(pairs), 3) if pairs else 0.0
    return {
        "symbols": symbols,
        "matrix": matrix,
        "most_correlated": pairs[:8],
        "average": avg,
        "diversification": (
            "positions move together" if avg > 0.7 else "moderately linked" if avg > 0.4 else "well spread"
        ),
    }


def value_at_risk(equity_curve: list[dict[str, Any]], equity: float) -> dict[str, Any]:
    """Historical VaR and expected shortfall from the realised equity series."""
    values = [float(p.get("equity") or 0) for p in equity_curve if p.get("equity")]
    rets = _returns(values)
    if len(rets) < 20:
        return {"ok": False, "note": "need more equity history for a meaningful VaR", "samples": len(rets)}
    var95 = _percentile(rets, 0.05)
    var99 = _percentile(rets, 0.01)
    tail = [r for r in rets if r <= var95]
    es = sum(tail) / len(tail) if tail else var95
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1))
    return {
        "ok": True,
        "samples": len(rets),
        "var95_pct": round(var95 * 100, 3),
        "var99_pct": round(var99 * 100, 3),
        "var95_value": round(abs(var95) * equity, 2),
        "var99_value": round(abs(var99) * equity, 2),
        "expected_shortfall_pct": round(es * 100, 3),
        "volatility_pct": round(sd * 100, 3),
        "vol_pct": round(sd * 100, 3),
        "worst_pct": round(min(rets) * 100, 3),
        "best_pct": round(max(rets) * 100, 3),
    }


def r_distribution(fills: list[dict[str, Any]], buckets: Iterable[float] = (-3, -2, -1, 0, 1, 2, 3)) -> dict[str, Any]:
    """Where the money comes from, in R multiples."""
    rs = [float(f.get("r") or 0.0) for f in fills if f.get("r") is not None]
    rs = [r for r in rs if r != 0.0]
    if not rs:
        return {"ok": False, "trades": 0, "buckets": []}
    edges = list(buckets)
    rows = []
    for i, edge in enumerate(edges):
        lo = edges[i - 1] if i else -math.inf
        rows.append({"label": f"{lo:g}..{edge:g}" if i else f"< {edge:g}", "count": sum(1 for r in rs if lo < r <= edge)})
    rows.append({"label": f"> {edges[-1]:g}", "count": sum(1 for r in rs if r > edges[-1])})
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    expectancy = sum(rs) / len(rs)
    top = sorted(rs, reverse=True)[: max(1, len(rs) // 10)]
    return {
        "ok": True,
        "trades": len(rs),
        "buckets": rows,
        "expectancy_r": round(expectancy, 3),
        "avg_win_r": round(sum(wins) / len(wins), 3) if wins else 0.0,
        "avg_loss_r": round(sum(losses) / len(losses), 3) if losses else 0.0,
        "best_r": round(max(rs), 2),
        "worst_r": round(min(rs), 2),
        "top_decile_share": round(sum(top) / sum(rs), 3) if sum(rs) else 0.0,
    }


def summary(
    positions: list[dict[str, Any]],
    equity: float,
    cash: float,
    equity_curve: list[dict[str, Any]],
    series: dict[str, list[float]],
    fills: list[dict[str, Any]],
    journal: Journal | None = None,
) -> dict[str, Any]:
    exp = exposure(positions, equity, cash)
    risk = open_risk(positions, equity)
    out = {
        "equity": round(equity, 2),
        "cash": round(cash, 2),
        "exposure": exp,
        "open_risk": risk,
        "correlations": correlations(series),
        "var": value_at_risk(equity_curve, equity),
        "r_distribution": r_distribution(fills),
        "warnings": [],
    }
    if exp["largest_pct"] > 25:
        out["warnings"].append(f"{exp['rows'][0]['symbol']} is {exp['largest_pct']:.0f}% of equity")
    if exp["concentration"] > 0.5 and exp["positions"] > 1:
        out["warnings"].append("book is concentrated in one name")
    if risk["unprotected"]:
        out["warnings"].append(f"no stop on {', '.join(risk['unprotected'][:3])}")
    if risk["pct_equity"] > 6:
        out["warnings"].append(f"open risk is {risk['pct_equity']:.1f}% of equity")
    if out["correlations"]["average"] > 0.7 and exp["positions"] > 2:
        out["warnings"].append("positions are highly correlated — this is one bet, not several")
    if journal:
        out["tags"] = journal.by_tag(fills)
        out["tag_list"] = journal.tags()
    return out
