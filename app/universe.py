"""Cross-venue instrument universe.

Holds every instrument from every venue/market, and answers the questions a
trader actually asks of a multi-exchange list:

* show me every USDT perp on MEXC sorted by volume,
* which coins are listed on all four venues,
* where is the widest cross-venue spread right now,
* who is paying funding, and how far is the perp trading from spot.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable

from app.market.venues import MARKETS, VENUES, fetch_all

log = logging.getLogger("universe")

SORTS = {
    "volume": ("volume_usd", True),
    "change": ("change_pct", True),
    "losers": ("change_pct", False),
    "price": ("last", True),
    "funding": ("funding_rate", True),
    "open_interest": ("open_interest", True),
    "symbol": ("symbol", False),
}

REFRESH_TTL = 900.0  # 15 minutes


class Universe:
    """In-memory index over every venue catalog."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.report: dict[str, Any] = {"source": "none", "ok": [], "failed": {}, "count": 0}
        self.updated = 0.0
        self.loading = False

    # -- loading ---------------------------------------------------------- #
    @property
    def stale(self) -> bool:
        return not self.rows or (time.time() - self.updated) > REFRESH_TTL

    async def refresh(
        self,
        venues: Iterable[str] = VENUES,
        markets: Iterable[str] = MARKETS,
        force: bool = False,
    ) -> dict[str, Any]:
        if self.loading or (not force and not self.stale):
            return self.report
        self.loading = True
        try:
            rows, report = await fetch_all(venues, markets)
        except Exception as exc:  # pragma: no cover - network only
            log.warning("universe refresh failed: %s", exc)
            self.loading = False
            return self.report
        self.loading = False
        if not rows:
            return self.report
        scope = {(v, m) for v in venues for m in markets}
        if self.rows and scope != {(v, m) for v in VENUES for m in MARKETS}:
            # scoped refresh: swap only the partitions we just pulled
            kept = [r for r in self.rows if (r["venue"], r["market"]) not in scope]
            rows = kept + rows
            merged = dict(self.report)
            merged["failed"] = {**merged.get("failed", {}), **report.get("failed", {})}
            ok = {f"{o['venue']}:{o['market']}": o for o in merged.get("ok", [])}
            for o in report.get("ok", []):
                ok[f"{o['venue']}:{o['market']}"] = o
            merged["ok"] = list(ok.values())
            merged["source"] = report.get("source", merged.get("source"))
            merged["note"] = report.get("note")
            merged["count"] = len(rows)
            merged["elapsed_ms"] = report.get("elapsed_ms")
            report = merged
        self.rows = rows
        self.report = report
        self.updated = time.time()
        log.info(
            "universe: %d instruments from %d catalogs (%s)",
            len(rows),
            len(report.get("ok") or []),
            report.get("source"),
        )
        return report

    # -- queries ---------------------------------------------------------- #
    def query(
        self,
        venue: str = "",
        market: str = "",
        quote: str = "",
        search: str = "",
        sort: str = "volume",
        limit: int = 100,
        min_volume: float = 0.0,
        offset: int = 0,
    ) -> dict[str, Any]:
        rows = self.rows
        venues = {v.strip().lower() for v in venue.split(",") if v.strip()} if venue else set()
        markets = {m.strip().lower() for m in market.split(",") if m.strip()} if market else set()
        quotes = {q.strip().upper() for q in quote.split(",") if q.strip()} if quote else set()
        needle = (search or "").strip().upper()

        out = []
        for r in rows:
            if venues and r["venue"] not in venues:
                continue
            if markets and r["market"] not in markets:
                continue
            if quotes and r["quote"] not in quotes:
                continue
            if min_volume and (r["volume_usd"] or 0) < min_volume:
                continue
            if needle and needle not in r["symbol"] and needle not in r["base"]:
                continue
            out.append(r)

        key, desc = SORTS.get(sort, SORTS["volume"])
        out.sort(key=lambda r: (r.get(key) is None, r.get(key) or (0 if key != "symbol" else "")), reverse=desc)
        total = len(out)
        page = out[offset : offset + max(1, limit)]
        return {
            "rows": page,
            "total": total,
            "offset": offset,
            "limit": limit,
            "sort": sort,
            "source": self.report.get("source"),
            "updated": self.updated,
        }

    def find(self, symbol: str, venue: str = "", market: str = "") -> list[dict[str, Any]]:
        sym = symbol.upper().replace("-", "/")
        out = [r for r in self.rows if r["symbol"] == sym]
        if venue:
            out = [r for r in out if r["venue"] == venue]
        if market:
            out = [r for r in out if r["market"] == market]
        return out

    # -- aggregations ----------------------------------------------------- #
    def coins(self, quote: str = "USDT", limit: int = 100, min_venues: int = 1) -> list[dict[str, Any]]:
        """One row per base asset, merged across venues and markets."""
        buckets: dict[str, list[dict[str, Any]]] = {}
        for r in self.rows:
            if quote and r["quote"] != quote.upper():
                continue
            buckets.setdefault(r["base"], []).append(r)
        out = []
        for base, group in buckets.items():
            venues = sorted({r["venue"] for r in group})
            if len(venues) < min_venues:
                continue
            spot = [r for r in group if r["market"] == "spot"]
            perp = [r for r in group if r["market"] == "futures"]
            prices = [r["last"] for r in group if r["last"] > 0]
            volume = sum(r["volume_usd"] or 0 for r in group)
            fundings = [r["funding_rate"] for r in perp if r.get("funding_rate") is not None]
            oi = [r["open_interest"] for r in perp if r.get("open_interest")]
            ref = max(group, key=lambda r: r["volume_usd"] or 0)
            out.append(
                {
                    "base": base,
                    "quote": quote.upper(),
                    "symbol": f"{base}/{quote.upper()}",
                    "venues": venues,
                    "venue_count": len(venues),
                    "markets": sorted({r["market"] for r in group}),
                    "listings": len(group),
                    "spot_venues": len({r["venue"] for r in spot}),
                    "perp_venues": len({r["venue"] for r in perp}),
                    "last": ref["last"],
                    "change_pct": round(sum(r["change_pct"] for r in group) / len(group), 3),
                    "volume_usd": round(volume, 2),
                    "spread_pct": round((max(prices) / min(prices) - 1) * 100, 4) if len(prices) > 1 else 0.0,
                    "avg_funding": round(sum(fundings) / len(fundings), 8) if fundings else None,
                    "open_interest": round(sum(oi), 2) if oi else None,
                    "best_venue": ref["venue"],
                }
            )
        out.sort(key=lambda r: -r["volume_usd"])
        return out[:limit]

    def arbitrage(self, quote: str = "USDT", market: str = "spot", limit: int = 20, min_volume: float = 1e6):
        """Widest same-symbol price gaps between venues."""
        buckets: dict[str, list[dict[str, Any]]] = {}
        for r in self.rows:
            if r["quote"] != quote.upper() or r["market"] != market:
                continue
            if (r["volume_usd"] or 0) < min_volume or r["last"] <= 0:
                continue
            buckets.setdefault(r["base"], []).append(r)
        out = []
        for base, group in buckets.items():
            if len(group) < 2:
                continue
            cheap = min(group, key=lambda r: r["last"])
            rich = max(group, key=lambda r: r["last"])
            gap = (rich["last"] / cheap["last"] - 1) * 100
            if gap <= 0:
                continue
            out.append(
                {
                    "base": base,
                    "symbol": f"{base}/{quote.upper()}",
                    "market": market,
                    "buy_venue": cheap["venue"],
                    "buy_price": cheap["last"],
                    "sell_venue": rich["venue"],
                    "sell_price": rich["last"],
                    "spread_pct": round(gap, 4),
                    "venues": len(group),
                    "min_volume_usd": round(min(r["volume_usd"] or 0 for r in group), 2),
                }
            )
        out.sort(key=lambda r: -r["spread_pct"])
        return out[:limit]

    def funding(self, quote: str = "USDT", limit: int = 20, min_volume: float = 1e6) -> dict[str, Any]:
        """Funding extremes plus perp-vs-spot basis."""
        spot_px: dict[tuple[str, str], float] = {}
        for r in self.rows:
            if r["market"] == "spot" and r["quote"] == quote.upper() and r["last"] > 0:
                key = (r["venue"], r["base"])
                spot_px[key] = r["last"]
        best_spot: dict[str, float] = {}
        for (_, base), px in spot_px.items():
            best_spot.setdefault(base, px)

        rows = []
        for r in self.rows:
            if r["market"] != "futures" or r["quote"] != quote.upper():
                continue
            if (r["volume_usd"] or 0) < min_volume:
                continue
            spot = spot_px.get((r["venue"], r["base"])) or best_spot.get(r["base"])
            basis = round((r["last"] / spot - 1) * 100, 4) if spot else None
            rows.append(
                {
                    "symbol": r["symbol"],
                    "base": r["base"],
                    "venue": r["venue"],
                    "last": r["last"],
                    "funding_rate": r.get("funding_rate"),
                    "funding_apr": round((r["funding_rate"] or 0) * 3 * 365 * 100, 3)
                    if r.get("funding_rate") is not None
                    else None,
                    "open_interest": r.get("open_interest"),
                    "volume_usd": r["volume_usd"],
                    "spot": spot,
                    "basis_pct": basis,
                }
            )
        priced = [r for r in rows if r["funding_rate"] is not None]
        priced.sort(key=lambda r: -(r["funding_rate"] or 0))
        by_basis = [r for r in rows if r["basis_pct"] is not None]
        by_basis.sort(key=lambda r: -(r["basis_pct"] or 0))
        return {
            "longs_pay": priced[:limit],
            "shorts_pay": list(reversed(priced[-limit:])) if priced else [],
            "premium": by_basis[:limit],
            "discount": list(reversed(by_basis[-limit:])) if by_basis else [],
            "count": len(rows),
        }

    def stats(self) -> dict[str, Any]:
        by_venue: dict[str, dict[str, Any]] = {}
        quotes: dict[str, int] = {}
        bases: set[str] = set()
        spot = perp = 0
        volume = 0.0
        for r in self.rows:
            v = by_venue.setdefault(
                r["venue"], {"venue": r["venue"], "spot": 0, "futures": 0, "volume_usd": 0.0, "bases": set()}
            )
            v[r["market"]] = v.get(r["market"], 0) + 1
            v["volume_usd"] += r["volume_usd"] or 0
            v["bases"].add(r["base"])
            quotes[r["quote"]] = quotes.get(r["quote"], 0) + 1
            bases.add(r["base"])
            volume += r["volume_usd"] or 0
            if r["market"] == "spot":
                spot += 1
            else:
                perp += 1
        venue_rows = []
        for v in by_venue.values():
            venue_rows.append(
                {
                    "venue": v["venue"],
                    "spot": v["spot"],
                    "futures": v["futures"],
                    "total": v["spot"] + v["futures"],
                    "coins": len(v["bases"]),
                    "volume_usd": round(v["volume_usd"], 2),
                }
            )
        venue_rows.sort(key=lambda r: -r["total"])
        return {
            "instruments": len(self.rows),
            "coins": len(bases),
            "spot": spot,
            "futures": perp,
            "volume_usd": round(volume, 2),
            "venues": venue_rows,
            "quotes": sorted(
                ({"quote": q, "count": c} for q, c in quotes.items()), key=lambda r: -r["count"]
            )[:10],
            "source": self.report.get("source", "none"),
            "note": self.report.get("note"),
            "failed": self.report.get("failed", {}),
            "updated": self.updated,
            "age_sec": round(time.time() - self.updated, 1) if self.updated else None,
        }

    def to_csv(self, rows: list[dict[str, Any]]) -> str:
        cols = [
            "venue", "market", "symbol", "base", "quote", "last", "change_pct",
            "volume_usd", "funding_rate", "open_interest", "contract", "source",
        ]
        lines = [",".join(cols)]
        for r in rows:
            lines.append(",".join("" if r.get(c) is None else str(r.get(c)) for c in cols))
        return "\n".join(lines)
