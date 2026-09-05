"""Bundled offline instrument catalog.

Only used when every venue REST endpoint is unreachable (air-gapped hosts,
filtered networks, exchange outages). Prices are simulated and every row is
tagged ``source: "offline"`` so the UI can say so out loud — this exists to keep
the terminal usable, never to pass fake data off as market data.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

# base assets, roughly ordered by how much volume they usually carry
MAJORS = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT",
    "TON", "TRX", "MATIC", "LTC", "BCH", "NEAR", "UNI", "ATOM", "APT", "SUI",
    "ARB", "OP", "FIL", "INJ", "TIA", "SEI", "AAVE", "STX", "RUNE", "IMX",
]

MID_CAPS = [
    "PEPE", "SHIB", "WIF", "BONK", "FLOKI", "ORDI", "JUP", "PYTH", "JTO", "WLD",
    "FET", "RNDR", "GRT", "SAND", "MANA", "AXS", "GALA", "CHZ", "ENJ", "APE",
    "CRV", "MKR", "SNX", "COMP", "LDO", "DYDX", "GMX", "1INCH", "SUSHI", "CAKE",
    "ALGO", "VET", "HBAR", "EGLD", "FTM", "THETA", "EOS", "XTZ", "NEO", "IOTA",
    "KAVA", "ZIL", "ONE", "QNT", "FLOW", "MINA", "ROSE", "CELO", "ANKR", "SKL",
]

LONG_TAIL = [
    "BLUR", "ID", "ARKM", "MAGIC", "HIGH", "ACE", "NFP", "AI", "XAI", "MANTA",
    "ALT", "PIXEL", "PORTAL", "AEVO", "ETHFI", "ENA", "W", "TNSR", "SAGA", "OMNI",
    "REZ", "BB", "NOT", "IO", "ZK", "LISTA", "ZRO", "G", "BANANA", "RARE",
    "MOVE", "ME", "PENGU", "USUAL", "VANA", "ANIME", "BERA", "TRUMP", "MELANIA", "KAITO",
]

# who lists what (MEXC is the long-tail venue, Binance the most curated)
VENUE_COVERAGE: dict[str, dict[str, Any]] = {
    "binance": {"majors": 1.0, "mid": 0.85, "tail": 0.35, "quotes": ["USDT", "USDC", "BTC", "FDUSD"]},
    "bybit": {"majors": 1.0, "mid": 0.75, "tail": 0.45, "quotes": ["USDT", "USDC"]},
    "okx": {"majors": 1.0, "mid": 0.7, "tail": 0.3, "quotes": ["USDT", "USDC", "BTC"]},
    "mexc": {"majors": 1.0, "mid": 0.95, "tail": 0.95, "quotes": ["USDT", "USDC"]},
}

# futures are listed for fewer assets than spot
FUTURES_RATIO = {"binance": 0.75, "bybit": 0.8, "okx": 0.6, "mexc": 0.85}


def _hash(*parts: str) -> float:
    digest = hashlib.sha256("|".join(parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _price(base: str, quote: str) -> float:
    from app.market.feeds import _base_price

    px = _base_price(f"{base}/USDT")
    if quote == "BTC":
        return px / _base_price("BTC/USDT")
    if quote == "ETH":
        return px / _base_price("ETH/USDT")
    return px


def _volume(base: str, venue: str, market: str, quote: str, tier: int) -> float:
    scale = {0: 9.4, 1: 7.8, 2: 6.4}[tier]
    jitter = _hash(base, venue, market, quote)
    venue_mult = {"binance": 1.0, "bybit": 0.55, "okx": 0.5, "mexc": 0.35}[venue]
    quote_mult = {"USDT": 1.0, "USDC": 0.28, "FDUSD": 0.22, "BTC": 0.09, "ETH": 0.06}.get(quote, 0.1)
    mult = venue_mult * quote_mult * (0.55 if market == "futures" else 1.0)
    return (10**scale) * (0.35 + jitter) * mult


def offline_catalog(
    venues: Iterable[str] = ("binance", "bybit", "okx", "mexc"),
    markets: Iterable[str] = ("spot", "futures"),
) -> list[dict[str, Any]]:
    """Deterministic instrument list used when no venue can be reached."""
    from app.market.venues import instrument

    tiers = [(MAJORS, 0, "majors"), (MID_CAPS, 1, "mid"), (LONG_TAIL, 2, "tail")]
    rows: list[dict[str, Any]] = []
    for venue in venues:
        cov = VENUE_COVERAGE.get(venue)
        if not cov:
            continue
        for assets, tier, key in tiers:
            for base in assets:
                if _hash(venue, base, "listed") > cov[key]:
                    continue
                for quote in cov["quotes"]:
                    if quote != "USDT" and _hash(venue, base, quote) > (0.5 if tier == 0 else 0.12):
                        continue
                    last = _price(base, quote)
                    if last <= 0:
                        continue
                    change = (_hash(base, venue, quote, "chg") - 0.5) * 14.0
                    for market in markets:
                        if market == "futures":
                            if quote not in ("USDT", "USDC"):
                                continue
                            if _hash(venue, base, "perp") > FUTURES_RATIO.get(venue, 0.7):
                                continue
                        vol = _volume(base, venue, market, quote, tier)
                        funding = None
                        oi = None
                        if market == "futures":
                            funding = round((_hash(base, venue, "fund") - 0.45) * 0.0009, 8)
                            oi = vol * (0.18 + _hash(base, venue, "oi") * 0.5)
                        rows.append(
                            instrument(
                                venue,
                                market,
                                base,
                                quote,
                                f"{base}{quote}",
                                last * (1.0 + (_hash(base, venue, market, "px") - 0.5) * 0.004),
                                change + (_hash(venue, market, base, "d") - 0.5) * 0.8,
                                vol,
                                last * 1.03,
                                last * 0.97,
                                funding_rate=funding,
                                open_interest=oi,
                                source="offline",
                            )
                        )
    return rows
