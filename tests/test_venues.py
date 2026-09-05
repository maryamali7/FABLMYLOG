"""Venue parsers, exercised against recorded sample payloads.

The sandbox blocks outbound TLS, so these fixtures are trimmed copies of the
documented public responses. They lock in the field mapping for each venue and
prove one malformed row can never take out a whole catalog.
"""

import asyncio
import json

import httpx
import pytest

from app.market import venues as V

SAMPLES = {
    "https://api.binance.com/api/v3/ticker/24hr": [
        {"symbol": "BTCUSDT", "lastPrice": "64000.10", "priceChangePercent": "1.250",
         "quoteVolume": "1200000000.0", "highPrice": "65000.0", "lowPrice": "63000.0"},
        {"symbol": "ETHFDUSD", "lastPrice": "3200.5", "priceChangePercent": "-0.80",
         "quoteVolume": "45000000.0", "highPrice": "3300", "lowPrice": "3150"},
        {"symbol": "SOMETHINGWEIRD", "lastPrice": "0", "priceChangePercent": "x"},
        {"symbol": "BADROW"},
    ],
    "https://fapi.binance.com/fapi/v1/ticker/24hr": [
        {"symbol": "BTCUSDT", "lastPrice": "64010.0", "priceChangePercent": "1.30",
         "quoteVolume": "9000000000.0", "highPrice": "65100", "lowPrice": "63010"},
    ],
    "https://fapi.binance.com/fapi/v1/premiumIndex": [
        {"symbol": "BTCUSDT", "lastFundingRate": "0.00012500"},
    ],
    "https://api.bybit.com/v5/market/tickers": {
        "result": {"list": [
            {"symbol": "BTCUSDT", "lastPrice": "64005", "price24hPcnt": "0.0125",
             "turnover24h": "500000000", "highPrice24h": "65000", "lowPrice24h": "63000",
             "fundingRate": "0.0001", "openInterestValue": "1500000000"},
            {"symbol": "NOTAPAIR", "lastPrice": "1"},
        ]}
    },
    "https://www.okx.com/api/v5/market/tickers": {
        "data": [
            {"instId": "BTC-USDT", "last": "64020", "open24h": "63000",
             "volCcy24h": "800000000", "high24h": "65000", "low24h": "62800"},
            {"instId": "SOL-USDT-SWAP", "last": "150.2", "open24h": "148.0",
             "volCcy24h": "9000000", "high24h": "152", "low24h": "146"},
        ]
    },
    "https://www.okx.com/api/v5/public/open-interest": {
        "data": [{"instId": "SOL-USDT-SWAP", "oiCcy": "1200000"}]
    },
    "https://api.mexc.com/api/v3/ticker/24hr": [
        {"symbol": "PEPEUSDT", "lastPrice": "0.0000082", "priceChangePercent": "0.0345",
         "quoteVolume": "31000000", "highPrice": "0.0000090", "lowPrice": "0.0000079"},
    ],
    "https://contract.mexc.com/api/v1/contract/ticker": {
        "data": [
            {"symbol": "BTC_USDT", "lastPrice": 64030.5, "riseFallRate": 0.0131,
             "amount24": 2100000000, "high24Price": 65020, "lower24Price": 63040,
             "fundingRate": 0.00008, "holdVol": 880000000},
        ]
    },
}


def handler(request: httpx.Request) -> httpx.Response:
    body = SAMPLES.get(str(request.url).split("?")[0])
    if body is None:
        return httpx.Response(404, json={"error": "no fixture"})
    return httpx.Response(200, content=json.dumps(body), headers={"content-type": "application/json"})


def run(fetcher):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetcher(client)

    return asyncio.run(go())


def test_binance_spot_parser_skips_junk_rows():
    rows = run(V.binance_spot)
    assert [r["symbol"] for r in rows] == ["BTC/USDT", "ETH/FDUSD"]
    btc = rows[0]
    assert btc["venue"] == "binance" and btc["market"] == "spot"
    assert btc["last"] == pytest.approx(64000.10)
    assert btc["change_pct"] == pytest.approx(1.25)
    assert btc["volume_usd"] == pytest.approx(1.2e9)
    assert btc["funding_rate"] is None and btc["contract"] == "spot"


def test_binance_futures_joins_funding():
    rows = run(V.binance_futures)
    assert len(rows) == 1
    assert rows[0]["funding_rate"] == pytest.approx(0.000125)
    assert rows[0]["contract"] == "perpetual"
    assert rows[0]["id"] == "binance:futures:BTC/USDT"


def test_bybit_converts_fraction_to_percent():
    spot = run(V.bybit_spot)
    assert len(spot) == 1
    assert spot[0]["change_pct"] == pytest.approx(1.25)
    assert spot[0]["market"] == "spot"
    perp = run(V.bybit_futures)
    assert perp[0]["market"] == "futures"
    assert perp[0]["funding_rate"] == pytest.approx(0.0001)
    assert perp[0]["open_interest"] == pytest.approx(1.5e9)


def test_okx_computes_change_from_open_and_strips_swap_suffix():
    spot = run(V.okx_spot)
    assert [r["symbol"] for r in spot] == ["BTC/USDT", "SOL/USDT"]
    assert spot[0]["change_pct"] == pytest.approx((64020 / 63000 - 1) * 100, abs=1e-3)
    perp = run(V.okx_futures)
    sol = next(r for r in perp if r["base"] == "SOL")
    assert sol["symbol"] == "SOL/USDT" and sol["raw"] == "SOL-USDT-SWAP"
    assert sol["open_interest"] == pytest.approx(1200000)


def test_mexc_spot_and_futures():
    spot = run(V.mexc_spot)
    assert spot[0]["symbol"] == "PEPE/USDT"
    assert spot[0]["change_pct"] == pytest.approx(3.45, abs=1e-6)
    perp = run(V.mexc_futures)
    assert perp[0]["symbol"] == "BTC/USDT" and perp[0]["market"] == "futures"
    assert perp[0]["change_pct"] == pytest.approx(1.31, abs=1e-6)
    assert perp[0]["funding_rate"] == pytest.approx(0.00008)
    assert perp[0]["open_interest"] == pytest.approx(8.8e8)


def test_fetch_all_reports_failures_and_falls_back(monkeypatch):
    async def boom(_client):
        raise RuntimeError("connection reset")

    monkeypatch.setitem(V.FETCHERS, ("binance", "spot"), boom)
    monkeypatch.setitem(V.FETCHERS, ("binance", "futures"), boom)
    rows, report = asyncio.run(V.fetch_all(["binance"], ["spot", "futures"]))
    assert report["source"] == "offline"
    assert "binance:spot" in report["failed"]
    assert "connection reset" in report["failed"]["binance:spot"]
    assert report["note"]
    assert rows and all(r["source"] == "offline" for r in rows)
    assert report["count"] == len(rows)
    assert report["elapsed_ms"] >= 0


def test_fetch_all_keeps_partial_success(monkeypatch):
    async def boom(_client):
        raise RuntimeError("418 teapot")

    async def one(_client):
        return [V.instrument("okx", "spot", "BTC", "USDT", "BTC-USDT", 64000.0)]

    monkeypatch.setitem(V.FETCHERS, ("okx", "spot"), one)
    monkeypatch.setitem(V.FETCHERS, ("okx", "futures"), boom)
    rows, report = asyncio.run(V.fetch_all(["okx"], ["spot", "futures"]))
    assert report["source"] == "rest", "a partial success must not trigger the offline catalog"
    assert len(rows) == 1
    assert report["ok"] == [{"venue": "okx", "market": "spot", "count": 1}]
    assert "okx:futures" in report["failed"]
