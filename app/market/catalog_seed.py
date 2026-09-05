"""Bundled offline instrument catalog.

Only used when every venue REST endpoint is unreachable (air-gapped hosts,
filtered networks, exchange outages). Prices are simulated and every row is
tagged ``source: "offline"`` so the UI can say so out loud — this exists to keep
the terminal usable, never to pass fake data off as market data.

Coverage mirrors the real world roughly: Binance/OKX are curated, MEXC and Gate
carry the long tail, Bitget/KuCoin/HTX/Bybit sit in between, and coin-margined
(inverse) contracts exist only for the majors.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

# ---------------------------------------------------------------- base assets
MAJORS = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT",
    "TON", "TRX", "MATIC", "LTC", "BCH", "NEAR", "UNI", "ATOM", "APT", "SUI",
    "ARB", "OP", "FIL", "INJ", "TIA", "SEI", "AAVE", "STX", "RUNE", "IMX",
    "HBAR", "ICP", "ETC", "XLM", "VET", "ALGO", "FTM", "GRT", "RENDER", "PEPE",
]

MID_CAPS = [
    "SHIB", "WIF", "BONK", "FLOKI", "ORDI", "JUP", "PYTH", "JTO", "WLD", "FET",
    "SAND", "MANA", "AXS", "GALA", "CHZ", "ENJ", "APE", "CRV", "MKR", "SNX",
    "COMP", "LDO", "DYDX", "GMX", "1INCH", "SUSHI", "CAKE", "EGLD", "THETA", "EOS",
    "XTZ", "NEO", "IOTA", "KAVA", "ZIL", "ONE", "QNT", "FLOW", "MINA", "ROSE",
    "CELO", "ANKR", "SKL", "CFX", "KLAY", "WAVES", "ZEC", "DASH", "XEC", "RVN",
    "ONT", "QTUM", "ZEN", "SC", "STORJ", "AR", "HNT", "BAND", "OCEAN", "NMR",
    "LPT", "AUDIO", "REQ", "RLC", "RSR", "SXP", "TWT", "BAKE", "JASMY", "GMT",
    "DUSK", "AGLD", "ILV", "YGG", "BICO", "GLMR", "ASTR", "KSM", "MOVR", "WOO",
]

LONG_TAIL = [
    "BLUR", "ID", "ARKM", "MAGIC", "HIGH", "ACE", "NFP", "AI", "XAI", "MANTA",
    "ALT", "PIXEL", "PORTAL", "AEVO", "ETHFI", "ENA", "W", "TNSR", "SAGA", "OMNI",
    "REZ", "BB", "NOT", "IO", "ZK", "LISTA", "ZRO", "G", "BANANA", "RARE",
    "MOVE", "ME", "PENGU", "USUAL", "VANA", "ANIME", "BERA", "TRUMP", "MELANIA", "KAITO",
    "LUNC", "LUNA", "USTC", "GST", "ALPACA", "AUCTION", "BEL", "DODO", "FARM", "FIS",
    "FORTH", "FRONT", "FXS", "GHST", "IDEX", "JOE", "KNC", "LINA", "LIT", "LOKA",
    "LQTY", "MASK", "MDT", "MTL", "NKN", "OGN", "OM", "ORN", "OXT", "PERP",
    "POLYX", "POWR", "PROM", "PUNDIX", "QUICK", "RAD", "RDNT", "REEF", "REN", "RIF",
    "SFP", "SLP", "SNT", "SPELL", "STG", "STMX", "STPT", "SUN", "SUPER", "SYN",
    "SYS", "TLM", "TRB", "TRU", "TVK", "UMA", "UNFI", "UTK", "VIB", "VITE",
    "VOXEL", "WAXP", "WIN", "WNXM", "XVG", "XVS", "YFI", "ZRX", "BTG", "ICX",
]

MICRO_CAPS = [
    "SLERF", "BOME", "MEW", "POPCAT", "MOG", "TURBO", "BRETT", "NEIRO", "MOODENG", "GOAT",
    "ACT", "PNUT", "CHILLGUY", "LUCE", "FWOG", "MICHI", "BILLY", "GIGA", "SPX", "WOJAK",
    "ANDY", "LADYS", "SNEK", "AIDOGE", "TOSHI", "DEGEN", "HIGHER", "BASED", "TYBG", "AERO",
    "VIRTUAL", "AIXBT", "ZEREBRO", "GRIFFAIN", "ARC", "AI16Z", "SWARMS", "FARTCOIN", "BUZZ", "PIPPIN",
    "ELIZAOS", "VVV", "COOKIE", "SONIC", "LAYER", "IP", "KAIA", "SOLV", "RED", "PARTI",
    "NIL", "GUN", "BABY", "WAL", "SIGN", "INIT", "HYPER", "SXT", "OBOL", "DOOD",
    "SOPH", "SKATE", "LA", "HOME", "SPK", "RESOLV", "TAKER", "ERA", "SAHARA", "NEWT",
    "ICNT", "PROVE", "TREE", "SAPIEN", "STBL", "XPL", "LINEA", "AVNT", "OPEN", "PUMP",
]

TIERS = [
    (MAJORS, 0, "majors"),
    (MID_CAPS, 1, "mid"),
    (LONG_TAIL, 2, "tail"),
    (MICRO_CAPS, 3, "micro"),
]

# who lists what — MEXC and Gate are the long-tail venues, Binance the curated one
VENUE_COVERAGE: dict[str, dict[str, Any]] = {
    "binance": {"majors": 1.0, "mid": 0.9, "tail": 0.45, "micro": 0.2,
                "quotes": ["USDT", "USDC", "BTC", "FDUSD", "TRY", "EUR"]},
    "bybit": {"majors": 1.0, "mid": 0.8, "tail": 0.5, "micro": 0.45,
              "quotes": ["USDT", "USDC", "BTC", "EUR"]},
    "okx": {"majors": 1.0, "mid": 0.78, "tail": 0.4, "micro": 0.3,
            "quotes": ["USDT", "USDC", "BTC", "ETH"]},
    "mexc": {"majors": 1.0, "mid": 0.97, "tail": 0.95, "micro": 0.95,
             "quotes": ["USDT", "USDC"]},
    "gate": {"majors": 1.0, "mid": 0.95, "tail": 0.92, "micro": 0.9,
             "quotes": ["USDT", "USDC", "BTC", "ETH"]},
    "kucoin": {"majors": 1.0, "mid": 0.85, "tail": 0.65, "micro": 0.55,
               "quotes": ["USDT", "USDC", "BTC", "ETH"]},
    "bitget": {"majors": 1.0, "mid": 0.82, "tail": 0.6, "micro": 0.6,
               "quotes": ["USDT", "USDC", "BTC"]},
    "htx": {"majors": 1.0, "mid": 0.75, "tail": 0.5, "micro": 0.35,
            "quotes": ["USDT", "USDC", "BTC", "ETH"]},
}

# how much of a venue's spot book also has a linear perp
FUTURES_RATIO = {
    "binance": 0.72, "bybit": 0.85, "okx": 0.62, "mexc": 0.9,
    "gate": 0.88, "kucoin": 0.6, "bitget": 0.8, "htx": 0.55,
}

# coin-margined contracts exist for a handful of majors only
INVERSE_BASES = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ADA", "LTC", "BCH", "LINK",
                 "AVAX", "DOT", "TRX", "ETC", "FIL", "EOS", "XLM", "ATOM"]
INVERSE_VENUES = {"binance": 0.9, "bybit": 0.6, "okx": 0.8, "kucoin": 0.4,
                  "gate": 0.35, "bitget": 0.45, "htx": 0.5}

# a slice of the long tail lives on exactly one venue — that is real life, and
# the "venue exclusives" board depends on it
EXCLUSIVE_HOMES = {
    base: ("mexc", "gate", "bitget", "kucoin", "htx", "bybit")[i % 6]
    for i, base in enumerate(MICRO_CAPS[48:])
}

VOLUME_SCALE = {0: 9.5, 1: 8.1, 2: 6.9, 3: 6.2}
VENUE_VOLUME = {"binance": 1.0, "bybit": 0.55, "okx": 0.5, "mexc": 0.35,
                "gate": 0.3, "kucoin": 0.26, "bitget": 0.42, "htx": 0.22}
QUOTE_VOLUME = {"USDT": 1.0, "USDC": 0.28, "FDUSD": 0.22, "BTC": 0.09,
                "ETH": 0.06, "TRY": 0.05, "EUR": 0.04, "USD": 0.5}


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
    if quote == "TRY":
        return px * 34.2
    if quote == "EUR":
        return px * 0.92
    return px


def _volume(base: str, venue: str, market: str, quote: str, tier: int) -> float:
    jitter = _hash(base, venue, market, quote)
    mult = VENUE_VOLUME.get(venue, 0.3) * QUOTE_VOLUME.get(quote, 0.1)
    if market == "futures":
        mult *= 0.75
    elif market == "inverse":
        mult *= 0.18
    return (10 ** VOLUME_SCALE[tier]) * (0.35 + jitter) * mult


def _rows_for(venue: str, base: str, quote: str, tier: int, markets: set[str]) -> list[dict[str, Any]]:
    from app.market.venues import instrument

    ref = _price(base, quote)
    if ref <= 0:
        return []
    change = (_hash(base, venue, quote, "chg") - 0.5) * 14.0
    out = []
    for market in ("spot", "futures", "inverse"):
        if market not in markets:
            continue
        if market == "futures":
            if quote not in ("USDT", "USDC"):
                continue
            if _hash(venue, base, "perp") > FUTURES_RATIO.get(venue, 0.7):
                continue
        if market == "inverse":
            continue  # inverse rows are generated separately (USD-quoted)
        vol = _volume(base, venue, market, quote, tier)
        funding = oi = None
        if market == "futures":
            funding = round((_hash(base, venue, "fund") - 0.45) * 0.0009, 8)
            oi = round(vol * (0.18 + _hash(base, venue, "oi") * 0.5), 2)
        last = ref * (1.0 + (_hash(base, venue, market, "px") - 0.5) * 0.004)
        out.append(
            instrument(
                venue, market, base, quote, f"{base}{quote}",
                last,
                change + (_hash(venue, market, base, "d") - 0.5) * 0.8,
                vol,
                last * 1.03,
                last * 0.97,
                funding_rate=funding,
                open_interest=oi,
                source="offline",
            )
        )
    return out


def _inverse_rows(venue: str, markets: set[str]) -> list[dict[str, Any]]:
    from app.market.venues import instrument

    if "inverse" not in markets or venue not in INVERSE_VENUES:
        return []
    out = []
    for base in INVERSE_BASES:
        if _hash(venue, base, "inv") > INVERSE_VENUES[venue]:
            continue
        ref = _price(base, "USDT")
        last = ref * (1.0 + (_hash(base, venue, "invpx") - 0.5) * 0.006)
        vol = _volume(base, venue, "inverse", "USD", 0 if base in MAJORS[:10] else 1)
        funding = round((_hash(base, venue, "invfund") - 0.45) * 0.0011, 8)
        dated = _hash(venue, base, "dated") > 0.7
        out.append(
            instrument(
                venue, "inverse", base, "USD", f"{base}USD_PERP",
                last,
                (_hash(base, venue, "invchg") - 0.5) * 12.0,
                vol,
                last * 1.035,
                last * 0.965,
                funding_rate=funding,
                open_interest=round(vol * (0.2 + _hash(base, venue, "invoi") * 0.6), 2),
                contract="perpetual",
                source="offline",
            )
        )
        if dated:
            out.append(
                instrument(
                    venue, "inverse", base, "USD", f"{base}USD_240927",
                    last * (1 + 0.004 + _hash(base, venue, "basis") * 0.01),
                    (_hash(base, venue, "invchg2") - 0.5) * 11.0,
                    vol * 0.3,
                    last * 1.05,
                    last * 0.95,
                    contract="dated 240927",
                    source="offline",
                )
            )
    return out


def offline_catalog(
    venues: Iterable[str] = tuple(VENUE_COVERAGE),
    markets: Iterable[str] = ("spot", "futures", "inverse"),
) -> list[dict[str, Any]]:
    """Deterministic instrument list used when no venue can be reached."""
    want_markets = set(markets)
    rows: list[dict[str, Any]] = []
    for venue in venues:
        cov = VENUE_COVERAGE.get(venue)
        if not cov:
            continue
        for assets, tier, key in TIERS:
            for base in assets:
                home = EXCLUSIVE_HOMES.get(base)
                if home and home != venue:
                    continue
                if not home and _hash(venue, base, "listed") > cov[key]:
                    continue
                for quote in cov["quotes"]:
                    if quote != "USDT":
                        ceiling = 0.5 if tier == 0 else (0.16 if tier == 1 else 0.06)
                        if _hash(venue, base, quote) > ceiling:
                            continue
                    rows.extend(_rows_for(venue, base, quote, tier, want_markets))
        rows.extend(_inverse_rows(venue, want_markets))
    return rows
