from __future__ import annotations

# Canonical internal format is BASE/QUOTE e.g. BTC/USDT

COINBASE_USD_MAP = {
    "BTC/USDT": "BTC-USD",
    "ETH/USDT": "ETH-USD",
    "SOL/USDT": "SOL-USD",
    "XRP/USDT": "XRP-USD",
    "DOGE/USDT": "DOGE-USD",
    "ADA/USDT": "ADA-USD",
    "AVAX/USDT": "AVAX-USD",
    "LINK/USDT": "LINK-USD",
    "DOT/USDT": "DOT-USD",
    "LTC/USDT": "LTC-USD",
    "ATOM/USDT": "ATOM-USD",
    "UNI/USDT": "UNI-USD",
    "APT/USDT": "APT-USD",
    "SUI/USDT": "SUI-USD",
    "NEAR/USDT": "NEAR-USD",
    "FIL/USDT": "FIL-USD",
    "ARB/USDT": "ARB-USD",
    "OP/USDT": "OP-USD",
    "AAVE/USDT": "AAVE-USD",
    "SHIB/USDT": "SHIB-USD",
    "PEPE/USDT": "PEPE-USD",
}

KRAKEN_MAP = {
    "BTC/USDT": "XBT/USDT",
    "BTC/USD": "XBT/USD",
    "ETH/USDT": "ETH/USDT",
    "SOL/USDT": "SOL/USDT",
    "XRP/USDT": "XRP/USDT",
    "DOGE/USDT": "DOGE/USDT",
    "ADA/USDT": "ADA/USDT",
    "LTC/USDT": "LTC/USDT",
    "LINK/USDT": "LINK/USDT",
    "DOT/USDT": "DOT/USDT",
    "ATOM/USDT": "ATOM/USDT",
    "AVAX/USDT": "AVAX/USDT",
    "UNI/USDT": "UNI/USDT",
}


def compact(symbol: str) -> str:
    return symbol.replace("/", "").replace("-", "").upper()


def base_quote(symbol: str) -> tuple[str, str]:
    s = symbol.replace("-", "/").upper()
    if "/" in s:
        b, q = s.split("/", 1)
        return b, q
    for q in ("USDT", "USDC", "USD", "BUSD", "EUR"):
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)], q
    return s, "USDT"


def to_binance(symbol: str) -> str:
    return compact(symbol).lower()


def to_bybit(symbol: str) -> str:
    return compact(symbol)


def to_okx(symbol: str) -> str:
    b, q = base_quote(symbol)
    return f"{b}-{q}"


def to_coinbase(symbol: str) -> str:
    if symbol in COINBASE_USD_MAP:
        return COINBASE_USD_MAP[symbol]
    b, q = base_quote(symbol)
    q = "USD" if q in {"USDT", "USDC"} else q
    return f"{b}-{q}"


def to_kraken(symbol: str) -> str:
    if symbol in KRAKEN_MAP:
        return KRAKEN_MAP[symbol]
    b, q = base_quote(symbol)
    if b == "BTC":
        b = "XBT"
    return f"{b}/{q}"


def from_binance(sym: str) -> str:
    s = sym.upper()
    for q in ("USDT", "USDC", "BTC", "ETH", "BNB", "FDUSD"):
        if s.endswith(q) and len(s) > len(q):
            return f"{s[:-len(q)]}/{q}"
    return s


def from_okx(inst: str) -> str:
    return inst.replace("-", "/")


def from_coinbase(product: str) -> str:
    s = product.replace("-", "/")
    if s.endswith("/USD"):
        # map USD books onto USDT watchlist for the bot
        return s[:-3] + "USDT"
    return s


def from_kraken(pair: str) -> str:
    s = pair.replace("XBT", "BTC")
    if "/" not in s and s.endswith("USDT"):
        return f"{s[:-4]}/USDT"
    return s


def display_base(symbol: str) -> str:
    return base_quote(symbol)[0]
