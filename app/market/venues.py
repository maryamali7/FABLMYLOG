"""Venue instrument catalogs — Binance, Bybit, OKX and MEXC, spot and futures.

Each venue exposes a public, key-free REST endpoint that lists every tradable
instrument with a 24h ticker. This module normalizes all of them into one
shape so the rest of the app never has to care which venue a coin came from::

    {
      "id": "binance:futures:BTC/USDT",
      "venue": "binance", "market": "futures",
      "symbol": "BTC/USDT", "raw": "BTCUSDT",
      "base": "BTC", "quote": "USDT",
      "last": 64000.0, "change_pct": 1.2, "volume_usd": 1.2e10,
      "funding_rate": 0.0001, "open_interest": 1.1e9, "contract": "perpetual",
      "source": "rest",
    }

Parsers are deliberately defensive: venues change field names, and a single
bad row must never take out a whole catalog.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Iterable

import httpx

from app.market.catalog_seed import offline_catalog

log = logging.getLogger("venues")

VENUES = ("binance", "bybit", "okx", "mexc", "kucoin", "gate", "bitget", "htx")

# spot · linear (USDT/USDC-margined) perps · inverse (coin-margined) contracts
MARKETS = ("spot", "futures", "inverse")

# quotes that mean "linear/stable-margined"; anything settled in USD is inverse
STABLES = ("USDT", "USDC", "FDUSD", "TUSD", "DAI", "USDE")

# quote assets we care about (everything else is dropped to keep the index sane)
QUOTES = ("USDT", "USDC", "USD", "BTC", "ETH", "FDUSD", "EUR", "TRY", "BNB")

TIMEOUT = httpx.Timeout(20.0, connect=8.0)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out or out in (float("inf"), float("-inf")):
        return default
    return out


def _market_for(quote: str, default: str = "futures") -> str:
    """Coin-margined contracts are quoted in USD; stable-margined ones are linear."""
    return default if quote in STABLES else "inverse"


def _pct_maybe_fraction(value: Any) -> float:
    """MEXC returns 24h change as a fraction (0.0345 = +3.45%).

    A few endpoints have shipped it as a plain percent instead, so treat
    anything larger than 1 as already-percent rather than reporting +345%.
    """
    v = _f(value)
    return v * 100.0 if abs(v) <= 1 else v


def split_symbol(raw: str, quotes: Iterable[str] = QUOTES) -> tuple[str, str]:
    """BTCUSDT -> (BTC, USDT); BTC-USDT-SWAP -> (BTC, USDT)."""
    s = (raw or "").upper().replace("_", "-")
    for suffix in ("-SWAP", "-PERP", "-FUTURES"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    if "-" in s:
        parts = [p for p in s.split("-") if p]
        if len(parts) >= 2:
            return parts[0], parts[1]
    if "/" in s:
        base, quote = s.split("/", 1)
        return base, quote
    for q in sorted(quotes, key=len, reverse=True):
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)], q
    return s, ""


def instrument(
    venue: str,
    market: str,
    base: str,
    quote: str,
    raw: str,
    last: float,
    change_pct: float = 0.0,
    volume_usd: float = 0.0,
    high: float = 0.0,
    low: float = 0.0,
    funding_rate: float | None = None,
    open_interest: float | None = None,
    contract: str | None = None,
    source: str = "rest",
) -> dict[str, Any]:
    symbol = f"{base}/{quote}" if quote else base
    kind = contract or ("perpetual" if market in ("futures", "inverse") else "spot")
    # dated contracts share a symbol with the perp, so key them by their raw id
    ident = f"{venue}:{market}:{symbol}"
    if kind.startswith("dated"):
        ident = f"{ident}:{raw}"
    return {
        "id": ident,
        "venue": venue,
        "market": market,
        "symbol": symbol,
        "raw": raw,
        "base": base,
        "quote": quote,
        "last": round(last, 10),
        "change_pct": round(change_pct, 3),
        "volume_usd": round(volume_usd, 2),
        "high": round(high, 10),
        "low": round(low, 10),
        "funding_rate": funding_rate,
        "open_interest": open_interest,
        "contract": kind,
        "source": source,
    }


# --------------------------------------------------------------------------- #
# Binance
# --------------------------------------------------------------------------- #


async def binance_spot(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    r = await client.get("https://api.binance.com/api/v3/ticker/24hr")
    r.raise_for_status()
    out = []
    for row in r.json():
        raw = str(row.get("symbol") or "")
        base, quote = split_symbol(raw)
        if not quote or quote not in QUOTES:
            continue
        last = _f(row.get("lastPrice"))
        if last <= 0:
            continue
        out.append(
            instrument(
                "binance",
                "spot",
                base,
                quote,
                raw,
                last,
                _f(row.get("priceChangePercent")),
                _f(row.get("quoteVolume")),
                _f(row.get("highPrice")),
                _f(row.get("lowPrice")),
            )
        )
    return out


async def binance_futures(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    r = await client.get("https://fapi.binance.com/fapi/v1/ticker/24hr")
    r.raise_for_status()
    funding: dict[str, float] = {}
    oi: dict[str, float] = {}
    try:
        pr = await client.get("https://fapi.binance.com/fapi/v1/premiumIndex")
        pr.raise_for_status()
        for row in pr.json():
            funding[str(row.get("symbol"))] = _f(row.get("lastFundingRate"))
    except Exception as exc:
        log.debug("binance funding: %s", exc)
    out = []
    for row in r.json():
        raw = str(row.get("symbol") or "")
        base, quote = split_symbol(raw)
        if not quote or quote not in QUOTES:
            continue
        last = _f(row.get("lastPrice"))
        if last <= 0:
            continue
        out.append(
            instrument(
                "binance",
                "futures",
                base,
                quote,
                raw,
                last,
                _f(row.get("priceChangePercent")),
                _f(row.get("quoteVolume")),
                _f(row.get("highPrice")),
                _f(row.get("lowPrice")),
                funding_rate=funding.get(raw),
                open_interest=oi.get(raw),
            )
        )
    return out


async def binance_inverse(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """COIN-margined perps and dated futures (BTCUSD_PERP, ETHUSD_240628)."""
    r = await client.get("https://dapi.binance.com/dapi/v1/ticker/24hr")
    r.raise_for_status()
    funding: dict[str, float] = {}
    try:
        pr = await client.get("https://dapi.binance.com/dapi/v1/premiumIndex")
        pr.raise_for_status()
        for row in pr.json():
            funding[str(row.get("symbol"))] = _f(row.get("lastFundingRate"))
    except Exception as exc:
        log.debug("binance inverse funding: %s", exc)
    out = []
    for row in r.json():
        raw = str(row.get("symbol") or "")
        head = raw.split("_")[0]
        base, quote = split_symbol(head)
        if not quote:
            continue
        last = _f(row.get("lastPrice"))
        if last <= 0:
            continue
        expiry = raw.split("_")[1] if "_" in raw else "PERP"
        out.append(
            instrument(
                "binance",
                "inverse",
                base,
                quote,
                raw,
                last,
                _f(row.get("priceChangePercent")),
                _f(row.get("baseVolume")) * last,
                _f(row.get("highPrice")),
                _f(row.get("lowPrice")),
                funding_rate=funding.get(raw),
                contract="perpetual" if expiry == "PERP" else f"dated {expiry}",
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Bybit
# --------------------------------------------------------------------------- #


async def _bybit(client: httpx.AsyncClient, category: str, market: str) -> list[dict[str, Any]]:
    r = await client.get("https://api.bybit.com/v5/market/tickers", params={"category": category})
    r.raise_for_status()
    rows = ((r.json() or {}).get("result") or {}).get("list") or []
    out = []
    for row in rows:
        raw = str(row.get("symbol") or "")
        base, quote = split_symbol(raw)
        if not quote or quote not in QUOTES:
            continue
        last = _f(row.get("lastPrice"))
        if last <= 0:
            continue
        out.append(
            instrument(
                "bybit",
                market,
                base,
                quote,
                raw,
                last,
                _f(row.get("price24hPcnt")) * 100.0,
                _f(row.get("turnover24h")),
                _f(row.get("highPrice24h")),
                _f(row.get("lowPrice24h")),
                funding_rate=_f(row.get("fundingRate")) if market == "futures" else None,
                open_interest=_f(row.get("openInterestValue")) or None,
            )
        )
    return out


async def bybit_spot(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    return await _bybit(client, "spot", "spot")


async def bybit_futures(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    return await _bybit(client, "linear", "futures")


async def bybit_inverse(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    return await _bybit(client, "inverse", "inverse")


# --------------------------------------------------------------------------- #
# OKX
# --------------------------------------------------------------------------- #


async def _okx(client: httpx.AsyncClient, inst_type: str, market: str) -> list[dict[str, Any]]:
    r = await client.get("https://www.okx.com/api/v5/market/tickers", params={"instType": inst_type})
    r.raise_for_status()
    rows = (r.json() or {}).get("data") or []
    oi: dict[str, float] = {}
    if market == "futures":
        try:
            o = await client.get(
                "https://www.okx.com/api/v5/public/open-interest", params={"instType": inst_type}
            )
            o.raise_for_status()
            for row in (o.json() or {}).get("data") or []:
                oi[str(row.get("instId"))] = _f(row.get("oiCcy"))
        except Exception as exc:
            log.debug("okx open interest: %s", exc)
    out = []
    for row in rows:
        raw = str(row.get("instId") or "")
        base, quote = split_symbol(raw)
        if not quote or quote not in QUOTES:
            continue
        last = _f(row.get("last"))
        if last <= 0:
            continue
        open24 = _f(row.get("open24h"))
        change = ((last / open24 - 1) * 100.0) if open24 else 0.0
        out.append(
            instrument(
                "okx",
                market,
                base,
                quote,
                raw,
                last,
                change,
                _f(row.get("volCcy24h")) * (last if market == "futures" else 1.0)
                if market == "futures"
                else _f(row.get("volCcy24h")),
                _f(row.get("high24h")),
                _f(row.get("low24h")),
                open_interest=oi.get(raw),
            )
        )
    return out


async def okx_spot(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    return await _okx(client, "SPOT", "spot")


async def okx_futures(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    rows = await _okx(client, "SWAP", "futures")
    return [r for r in rows if r["quote"] in STABLES]


async def okx_inverse(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    rows = await _okx(client, "SWAP", "inverse")
    return [r for r in rows if r["quote"] not in STABLES]


# --------------------------------------------------------------------------- #
# MEXC
# --------------------------------------------------------------------------- #


async def mexc_spot(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    r = await client.get("https://api.mexc.com/api/v3/ticker/24hr")
    r.raise_for_status()
    out = []
    for row in r.json():
        raw = str(row.get("symbol") or "")
        base, quote = split_symbol(raw)
        if not quote or quote not in QUOTES:
            continue
        last = _f(row.get("lastPrice"))
        if last <= 0:
            continue
        out.append(
            instrument(
                "mexc",
                "spot",
                base,
                quote,
                raw,
                last,
                _pct_maybe_fraction(row.get("priceChangePercent")),
                _f(row.get("quoteVolume")),
                _f(row.get("highPrice")),
                _f(row.get("lowPrice")),
            )
        )
    return out


async def mexc_futures(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    r = await client.get("https://contract.mexc.com/api/v1/contract/ticker")
    r.raise_for_status()
    rows = (r.json() or {}).get("data") or []
    out = []
    for row in rows:
        raw = str(row.get("symbol") or "")
        base, quote = split_symbol(raw)
        if not quote or quote not in QUOTES:
            continue
        last = _f(row.get("lastPrice"))
        if last <= 0:
            continue
        out.append(
            instrument(
                "mexc",
                "futures",
                base,
                quote,
                raw,
                last,
                _f(row.get("riseFallRate")) * 100.0,
                _f(row.get("amount24")),
                _f(row.get("high24Price")),
                _f(row.get("lower24Price")),
                funding_rate=_f(row.get("fundingRate")) or None,
                open_interest=_f(row.get("holdVol")) or None,
            )
        )
    return out



# --------------------------------------------------------------------------- #
# KuCoin
# --------------------------------------------------------------------------- #

# KuCoin still calls bitcoin XBT on the derivatives side
_KUCOIN_ALIAS = {"XBT": "BTC"}


async def kucoin_spot(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    r = await client.get("https://api.kucoin.com/api/v1/market/allTickers")
    r.raise_for_status()
    rows = ((r.json() or {}).get("data") or {}).get("ticker") or []
    out = []
    for row in rows:
        raw = str(row.get("symbol") or "")
        base, quote = split_symbol(raw)
        if not quote or quote not in QUOTES:
            continue
        last = _f(row.get("last"))
        if last <= 0:
            continue
        out.append(
            instrument(
                "kucoin",
                "spot",
                _KUCOIN_ALIAS.get(base, base),
                quote,
                raw,
                last,
                _f(row.get("changeRate")) * 100.0,
                _f(row.get("volValue")),
                _f(row.get("high")),
                _f(row.get("low")),
            )
        )
    return out


async def _kucoin_contracts(client: httpx.AsyncClient, market: str) -> list[dict[str, Any]]:
    r = await client.get("https://api-futures.kucoin.com/api/v1/contracts/active")
    r.raise_for_status()
    out = []
    for row in (r.json() or {}).get("data") or []:
        raw = str(row.get("symbol") or "")
        base = _KUCOIN_ALIAS.get(str(row.get("baseCurrency") or "").upper(), str(row.get("baseCurrency") or "").upper())
        quote = str(row.get("quoteCurrency") or "").upper()
        if not base or quote not in QUOTES:
            continue
        kind = _market_for(quote)
        if kind != market:
            continue
        last = _f(row.get("lastTradePrice"))
        if last <= 0:
            continue
        out.append(
            instrument(
                "kucoin",
                market,
                base,
                quote,
                raw,
                last,
                _f(row.get("priceChgPct")) * 100.0,
                _f(row.get("turnoverOf24h")),
                _f(row.get("highPrice")),
                _f(row.get("lowPrice")),
                funding_rate=_f(row.get("fundingFeeRate")) or None,
                open_interest=_f(row.get("openInterest")) * last or None,
                contract="perpetual" if str(row.get("type") or "FFWCSX") == "FFWCSX" else "dated",
            )
        )
    return out


async def kucoin_futures(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    return await _kucoin_contracts(client, "futures")


async def kucoin_inverse(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    return await _kucoin_contracts(client, "inverse")


# --------------------------------------------------------------------------- #
# Gate.io
# --------------------------------------------------------------------------- #


async def gate_spot(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    r = await client.get("https://api.gateio.ws/api/v4/spot/tickers")
    r.raise_for_status()
    out = []
    for row in r.json():
        raw = str(row.get("currency_pair") or "")
        base, quote = split_symbol(raw)
        if not quote or quote not in QUOTES:
            continue
        last = _f(row.get("last"))
        if last <= 0:
            continue
        out.append(
            instrument(
                "gate",
                "spot",
                base,
                quote,
                raw,
                last,
                _f(row.get("change_percentage")),
                _f(row.get("quote_volume")),
                _f(row.get("high_24h")),
                _f(row.get("low_24h")),
            )
        )
    return out


async def _gate_futures(client: httpx.AsyncClient, settle: str, market: str) -> list[dict[str, Any]]:
    r = await client.get(f"https://api.gateio.ws/api/v4/futures/{settle}/tickers")
    r.raise_for_status()
    out = []
    for row in r.json():
        raw = str(row.get("contract") or "")
        base, quote = split_symbol(raw)
        if not quote or quote not in QUOTES:
            continue
        last = _f(row.get("last"))
        if last <= 0:
            continue
        volume = _f(row.get("volume_24h_quote")) or _f(row.get("volume_24h_settle")) * last
        out.append(
            instrument(
                "gate",
                market,
                base,
                quote,
                raw,
                last,
                _f(row.get("change_percentage")),
                volume,
                _f(row.get("high_24h")),
                _f(row.get("low_24h")),
                funding_rate=_f(row.get("funding_rate")) or None,
                open_interest=_f(row.get("total_size")) * last or None,
            )
        )
    return out


async def gate_futures(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    return await _gate_futures(client, "usdt", "futures")


async def gate_inverse(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    return await _gate_futures(client, "btc", "inverse")


# --------------------------------------------------------------------------- #
# Bitget
# --------------------------------------------------------------------------- #


async def bitget_spot(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    r = await client.get("https://api.bitget.com/api/v2/spot/market/tickers")
    r.raise_for_status()
    out = []
    for row in (r.json() or {}).get("data") or []:
        raw = str(row.get("symbol") or "")
        base, quote = split_symbol(raw)
        if not quote or quote not in QUOTES:
            continue
        last = _f(row.get("lastPr"))
        if last <= 0:
            continue
        out.append(
            instrument(
                "bitget",
                "spot",
                base,
                quote,
                raw,
                last,
                _pct_maybe_fraction(row.get("change24h")),
                _f(row.get("usdtVolume")),
                _f(row.get("high24h")),
                _f(row.get("low24h")),
            )
        )
    return out


async def _bitget_mix(client: httpx.AsyncClient, product: str, market: str) -> list[dict[str, Any]]:
    r = await client.get(
        "https://api.bitget.com/api/v2/mix/market/tickers", params={"productType": product}
    )
    r.raise_for_status()
    out = []
    for row in (r.json() or {}).get("data") or []:
        raw = str(row.get("symbol") or "")
        base, quote = split_symbol(raw)
        if not quote or quote not in QUOTES:
            continue
        last = _f(row.get("lastPr"))
        if last <= 0:
            continue
        out.append(
            instrument(
                "bitget",
                market,
                base,
                quote,
                raw,
                last,
                _pct_maybe_fraction(row.get("change24h")),
                _f(row.get("usdtVolume")),
                _f(row.get("high24h")),
                _f(row.get("low24h")),
                funding_rate=_f(row.get("fundingRate")) or None,
                open_interest=_f(row.get("holdingAmount")) * last or None,
            )
        )
    return out


async def bitget_futures(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    return await _bitget_mix(client, "USDT-FUTURES", "futures")


async def bitget_inverse(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    return await _bitget_mix(client, "COIN-FUTURES", "inverse")


# --------------------------------------------------------------------------- #
# HTX (Huobi)
# --------------------------------------------------------------------------- #


async def htx_spot(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    r = await client.get("https://api.huobi.pro/market/tickers")
    r.raise_for_status()
    out = []
    for row in (r.json() or {}).get("data") or []:
        raw = str(row.get("symbol") or "").upper()
        base, quote = split_symbol(raw)
        if not quote or quote not in QUOTES:
            continue
        last = _f(row.get("close"))
        open_px = _f(row.get("open"))
        if last <= 0:
            continue
        out.append(
            instrument(
                "htx",
                "spot",
                base,
                quote,
                raw,
                last,
                ((last / open_px - 1) * 100.0) if open_px else 0.0,
                _f(row.get("vol")),
                _f(row.get("high")),
                _f(row.get("low")),
            )
        )
    return out


async def htx_futures(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """USDT-margined swaps."""
    r = await client.get("https://api.hbdm.com/v2/linear-swap-ex/market/detail/batch_merged")
    r.raise_for_status()
    funding: dict[str, float] = {}
    try:
        fr = await client.get("https://api.hbdm.com/linear-swap-api/v1/swap_batch_funding_rate")
        fr.raise_for_status()
        for row in (fr.json() or {}).get("data") or []:
            funding[str(row.get("contract_code"))] = _f(row.get("funding_rate"))
    except Exception as exc:
        log.debug("htx funding: %s", exc)
    out = []
    for row in (r.json() or {}).get("ticks") or []:
        raw = str(row.get("contract_code") or "")
        base, quote = split_symbol(raw)
        if not quote or quote not in QUOTES:
            continue
        last = _f(row.get("close"))
        open_px = _f(row.get("open"))
        if last <= 0:
            continue
        out.append(
            instrument(
                "htx",
                _market_for(quote),
                base,
                quote,
                raw,
                last,
                ((last / open_px - 1) * 100.0) if open_px else 0.0,
                _f(row.get("trade_turnover")),
                _f(row.get("high")),
                _f(row.get("low")),
                funding_rate=funding.get(raw),
            )
        )
    return out


FETCHERS: dict[tuple[str, str], Callable[[httpx.AsyncClient], Any]] = {
    ("binance", "spot"): binance_spot,
    ("binance", "futures"): binance_futures,
    ("binance", "inverse"): binance_inverse,
    ("bybit", "spot"): bybit_spot,
    ("bybit", "futures"): bybit_futures,
    ("bybit", "inverse"): bybit_inverse,
    ("okx", "spot"): okx_spot,
    ("okx", "futures"): okx_futures,
    ("okx", "inverse"): okx_inverse,
    ("mexc", "spot"): mexc_spot,
    ("mexc", "futures"): mexc_futures,
    ("kucoin", "spot"): kucoin_spot,
    ("kucoin", "futures"): kucoin_futures,
    ("kucoin", "inverse"): kucoin_inverse,
    ("gate", "spot"): gate_spot,
    ("gate", "futures"): gate_futures,
    ("gate", "inverse"): gate_inverse,
    ("bitget", "spot"): bitget_spot,
    ("bitget", "futures"): bitget_futures,
    ("bitget", "inverse"): bitget_inverse,
    ("htx", "spot"): htx_spot,
    ("htx", "futures"): htx_futures,
}


async def fetch_all(
    venues: Iterable[str] = VENUES,
    markets: Iterable[str] = MARKETS,
    allow_offline: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Pull every catalog in parallel. Returns (instruments, report)."""
    started = time.time()
    wanted = [(v, m) for v in venues for m in markets if (v, m) in FETCHERS]
    report: dict[str, Any] = {"ok": [], "failed": {}, "source": "rest", "ts": time.time()}
    rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": "fablmylog/1.0"}) as client:
        results = await asyncio.gather(
            *[FETCHERS[key](client) for key in wanted], return_exceptions=True
        )
    for key, res in zip(wanted, results):
        label = f"{key[0]}:{key[1]}"
        if isinstance(res, Exception):
            report["failed"][label] = str(res)[:160] or res.__class__.__name__
            continue
        rows.extend(res)
        report["ok"].append({"venue": key[0], "market": key[1], "count": len(res)})
    if not rows and allow_offline:
        rows = offline_catalog(venues, markets)
        report["source"] = "offline"
        seen: dict[str, int] = {}
        for r in rows:
            seen[f"{r['venue']}:{r['market']}"] = seen.get(f"{r['venue']}:{r['market']}", 0) + 1
        report["ok"] = [
            {"venue": k.split(":")[0], "market": k.split(":")[1], "count": n, "offline": True}
            for k, n in sorted(seen.items())
        ]
        report["note"] = (
            "venue REST is unreachable from this host — showing the bundled offline "
            "catalog with simulated prices"
        )
    report["elapsed_ms"] = round((time.time() - started) * 1000, 1)
    report["count"] = len(rows)
    return rows, report
