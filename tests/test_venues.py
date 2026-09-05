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
    "https://dapi.binance.com/dapi/v1/ticker/24hr": [
        {"symbol": "BTCUSD_PERP", "lastPrice": "64100", "priceChangePercent": "1.10",
         "baseVolume": "12000", "highPrice": "65000", "lowPrice": "63000"},
        {"symbol": "ETHUSD_240927", "lastPrice": "3250", "priceChangePercent": "0.9",
         "baseVolume": "5000", "highPrice": "3300", "lowPrice": "3200"},
    ],
    "https://dapi.binance.com/dapi/v1/premiumIndex": [
        {"symbol": "BTCUSD_PERP", "lastFundingRate": "0.00009"},
    ],
    "https://api.kucoin.com/api/v1/market/allTickers": {
        "data": {"ticker": [
            {"symbol": "BTC-USDT", "last": "64050", "changeRate": "0.0118",
             "volValue": "300000000", "high": "65000", "low": "63000"},
        ]}
    },
    "https://api-futures.kucoin.com/api/v1/contracts/active": {
        "data": [
            {"symbol": "XBTUSDTM", "baseCurrency": "XBT", "quoteCurrency": "USDT", "type": "FFWCSX",
             "lastTradePrice": 64060, "priceChgPct": 0.012, "turnoverOf24h": 900000000,
             "fundingFeeRate": 0.00007, "openInterest": "5000", "highPrice": 65000, "lowPrice": 63100},
            {"symbol": "XBTUSDM", "baseCurrency": "XBT", "quoteCurrency": "USD", "type": "FFWCSX",
             "lastTradePrice": 64070, "priceChgPct": 0.011, "turnoverOf24h": 120000000,
             "fundingFeeRate": 0.0001, "openInterest": "800"},
        ]
    },
    "https://api.gateio.ws/api/v4/spot/tickers": [
        {"currency_pair": "PEPE_USDT", "last": "0.0000081", "change_percentage": "2.4",
         "quote_volume": "18000000", "high_24h": "0.0000085", "low_24h": "0.0000078"},
    ],
    "https://api.gateio.ws/api/v4/futures/usdt/tickers": [
        {"contract": "SOL_USDT", "last": "150.1", "change_percentage": "1.9",
         "volume_24h_quote": "220000000", "funding_rate": "0.00013", "total_size": "900000"},
    ],
    "https://api.gateio.ws/api/v4/futures/btc/tickers": [
        {"contract": "BTC_USD", "last": "64080", "change_percentage": "1.0",
         "volume_24h_quote": "9000000", "funding_rate": "0.00005", "total_size": "1200"},
    ],
    "https://api.bitget.com/api/v2/spot/market/tickers": {
        "data": [
            {"symbol": "ETHUSDT", "lastPr": "3210", "change24h": "0.0075",
             "usdtVolume": "88000000", "high24h": "3300", "low24h": "3180"},
        ]
    },
    "https://api.bitget.com/api/v2/mix/market/tickers": {
        "data": [
            {"symbol": "ETHUSDT", "lastPr": "3212", "change24h": "0.0080",
             "usdtVolume": "410000000", "fundingRate": "0.00011",
             "holdingAmount": "150000", "high24h": "3305", "low24h": "3185"},
        ]
    },
    "https://api.huobi.pro/market/tickers": {
        "data": [
            {"symbol": "btcusdt", "close": 64090, "open": 63500, "high": 65000,
             "low": 63200, "vol": 240000000},
        ]
    },
    "https://api.hbdm.com/v2/linear-swap-ex/market/detail/batch_merged": {
        "ticks": [
            {"contract_code": "BTC-USDT", "close": 64095, "open": 63400,
             "high": 65100, "low": 63300, "trade_turnover": 800000000},
        ]
    },
    "https://api.hbdm.com/linear-swap-api/v1/swap_batch_funding_rate": {
        "data": [{"contract_code": "BTC-USDT", "funding_rate": "0.00006"}]
    },
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


def test_binance_inverse_labels_perp_and_dated():
    rows = run(V.binance_inverse)
    assert [r["market"] for r in rows] == ["inverse", "inverse"]
    perp = next(r for r in rows if r["base"] == "BTC")
    dated = next(r for r in rows if r["base"] == "ETH")
    assert perp["quote"] == "USD" and perp["contract"] == "perpetual"
    assert perp["funding_rate"] == pytest.approx(0.00009)
    assert dated["contract"] == "dated 240927"
    assert dated["id"].endswith("ETHUSD_240927"), "dated contracts need their own id"
    assert perp["id"] != dated["id"]


def test_kucoin_maps_xbt_to_btc_and_splits_inverse():
    spot = run(V.kucoin_spot)
    assert spot[0]["symbol"] == "BTC/USDT"
    assert spot[0]["change_pct"] == pytest.approx(1.18)
    linear = run(V.kucoin_futures)
    assert [r["symbol"] for r in linear] == ["BTC/USDT"]
    assert linear[0]["base"] == "BTC" and linear[0]["raw"] == "XBTUSDTM"
    assert linear[0]["funding_rate"] == pytest.approx(0.00007)
    inverse = run(V.kucoin_inverse)
    assert [r["market"] for r in inverse] == ["inverse"]
    assert inverse[0]["symbol"] == "BTC/USD"


def test_gate_spot_and_both_futures_settlements():
    spot = run(V.gate_spot)
    assert spot[0]["symbol"] == "PEPE/USDT" and spot[0]["change_pct"] == pytest.approx(2.4)
    linear = run(V.gate_futures)
    assert linear[0]["symbol"] == "SOL/USDT" and linear[0]["market"] == "futures"
    assert linear[0]["funding_rate"] == pytest.approx(0.00013)
    inverse = run(V.gate_inverse)
    assert inverse[0]["symbol"] == "BTC/USD" and inverse[0]["market"] == "inverse"


def test_bitget_fraction_change_and_mix_fields():
    spot = run(V.bitget_spot)
    assert spot[0]["symbol"] == "ETH/USDT"
    assert spot[0]["change_pct"] == pytest.approx(0.75)
    perp = run(V.bitget_futures)
    assert perp[0]["market"] == "futures"
    assert perp[0]["funding_rate"] == pytest.approx(0.00011)
    assert perp[0]["open_interest"] == pytest.approx(150000 * 3212)


def test_htx_computes_change_from_open():
    spot = run(V.htx_spot)
    assert spot[0]["symbol"] == "BTC/USDT"
    assert spot[0]["change_pct"] == pytest.approx((64090 / 63500 - 1) * 100, abs=1e-3)
    perp = run(V.htx_futures)
    assert perp[0]["market"] == "futures"
    assert perp[0]["funding_rate"] == pytest.approx(0.00006)


def test_every_registered_catalog_is_callable():
    assert len(V.FETCHERS) == 22
    assert {k[0] for k in V.FETCHERS} == set(V.VENUES)
    assert {k[1] for k in V.FETCHERS} == set(V.MARKETS)
    for key, fn in V.FETCHERS.items():
        assert callable(fn), key


def test_market_classification_by_quote():
    assert V._market_for("USDT") == "futures"
    assert V._market_for("USDC") == "futures"
    assert V._market_for("USD") == "inverse"
    assert V._market_for("BTC") == "inverse"
