import asyncio

import pytest

from app.market.catalog_seed import offline_catalog
from app.market.venues import MARKETS, VENUES, instrument, split_symbol
from app.universe import Universe


def build() -> Universe:
    u = Universe()
    u.rows = offline_catalog()
    u.report = {"source": "offline", "ok": [], "failed": {}, "count": len(u.rows)}
    u.updated = 1e12  # far future so it never looks stale mid-test
    return u


def test_split_symbol_handles_every_quote():
    assert split_symbol("BTCUSDT") == ("BTC", "USDT")
    assert split_symbol("ETHFDUSD") == ("ETH", "FDUSD")
    assert split_symbol("SOL-USDT-SWAP") == ("SOL", "USDT")
    assert split_symbol("BTC_USDT") == ("BTC", "USDT")
    assert split_symbol("ETHBTC") == ("ETH", "BTC")


def test_instrument_shape_is_canonical():
    row = instrument("mexc", "futures", "SOL", "USDT", "SOL_USDT", 150.0, 2.5, 1e9, 155, 145,
                     funding_rate=0.0001, open_interest=5e8, source="rest")
    assert row["id"] == "mexc:futures:SOL/USDT"
    assert row["symbol"] == "SOL/USDT" and row["raw"] == "SOL_USDT"
    assert row["contract"] == "perpetual"
    for key in ("venue", "market", "base", "quote", "last", "change_pct", "volume_usd",
                "high", "low", "funding_rate", "open_interest", "source"):
        assert key in row


def test_offline_catalog_covers_all_venues_and_markets():
    rows = offline_catalog()
    assert len(rows) > 3000, "the offline catalog should look like a real multi-venue book"
    assert len({r["base"] for r in rows}) > 250
    assert {r["venue"] for r in rows} == set(VENUES)
    assert {r["market"] for r in rows} == set(MARKETS)
    assert all(r["source"] == "offline" for r in rows), "offline rows must be flagged"
    for venue in VENUES:
        for market in ("spot", "futures"):
            assert any(r["venue"] == venue and r["market"] == market for r in rows), f"{venue}:{market}"
    # coin-margined books exist, but only where the venue actually runs them
    inverse = [r for r in rows if r["market"] == "inverse"]
    assert inverse and all(r["quote"] == "USD" for r in inverse)
    assert len({r["venue"] for r in inverse}) >= 5
    # futures rows carry derivative-only fields
    perps = [r for r in rows if r["market"] == "futures"]
    assert all(r["funding_rate"] is not None and r["open_interest"] for r in perps)
    # every row has a usable price and a unique id
    assert all(r["last"] > 0 for r in rows)
    assert len({r["id"] for r in rows}) == len(rows)


def test_offline_catalog_is_deterministic():
    assert [r["id"] for r in offline_catalog()] == [r["id"] for r in offline_catalog()]


def test_query_filters_and_sorts():
    u = build()
    everything = u.query(limit=5000)
    assert everything["total"] == len(u.rows)

    perps = u.query(market="futures", limit=1000)
    assert perps["total"] > 0
    assert all(r["market"] == "futures" for r in perps["rows"])

    mexc = u.query(venue="mexc", limit=1000)
    assert all(r["venue"] == "mexc" for r in mexc["rows"])

    combo = u.query(venue="binance,bybit", market="spot", quote="USDT", limit=1000)
    assert all(r["venue"] in ("binance", "bybit") and r["market"] == "spot" and r["quote"] == "USDT"
               for r in combo["rows"])

    vols = [r["volume_usd"] for r in u.query(sort="volume", limit=40)["rows"]]
    assert vols == sorted(vols, reverse=True)
    gainers = [r["change_pct"] for r in u.query(sort="change", limit=40)["rows"]]
    assert gainers == sorted(gainers, reverse=True)
    losers = [r["change_pct"] for r in u.query(sort="losers", limit=40)["rows"]]
    assert losers == sorted(losers)


def test_query_search_and_paging():
    u = build()
    hit = u.query(search="btc", limit=100)
    assert hit["total"] > 0
    assert all("BTC" in r["symbol"] for r in hit["rows"])

    first = u.query(limit=10)
    second = u.query(limit=10, offset=10)
    assert first["rows"][0]["id"] != second["rows"][0]["id"]
    assert second["offset"] == 10

    floor = u.query(min_volume=1e9, limit=500)
    assert all(r["volume_usd"] >= 1e9 for r in floor["rows"])


def test_coins_merge_across_venues():
    u = build()
    coins = u.coins(limit=200)
    assert coins
    btc = next(c for c in coins if c["base"] == "BTC")
    assert btc["venue_count"] >= 2
    assert btc["listings"] >= btc["venue_count"]
    assert set(btc["venues"]) <= set(VENUES)
    assert btc["spread_pct"] >= 0
    # sorted by aggregate volume
    vols = [c["volume_usd"] for c in coins]
    assert vols == sorted(vols, reverse=True)
    # min_venues filter really filters
    strict = u.coins(limit=200, min_venues=4)
    assert all(c["venue_count"] >= 4 for c in strict)
    assert len(strict) <= len(coins)


def test_arbitrage_pairs_cheapest_with_richest():
    u = build()
    rows = u.arbitrage(limit=10, min_volume=0)
    assert rows
    for r in rows:
        assert r["sell_price"] >= r["buy_price"]
        assert r["buy_venue"] != r["sell_venue"]
        assert r["spread_pct"] > 0
    assert [r["spread_pct"] for r in rows] == sorted([r["spread_pct"] for r in rows], reverse=True)


def test_funding_view_has_both_sides_and_basis():
    u = build()
    data = u.funding(limit=10, min_volume=0)
    assert data["count"] > 0
    assert data["longs_pay"] and data["shorts_pay"]
    top = data["longs_pay"][0]
    assert top["funding_rate"] >= data["shorts_pay"][0]["funding_rate"]
    assert top["funding_apr"] == pytest.approx(top["funding_rate"] * 3 * 365 * 100, abs=1e-3)
    with_basis = [r for r in data["premium"] if r["basis_pct"] is not None]
    assert with_basis, "perps should be joined against spot"


def test_stats_totals_match_rows():
    u = build()
    st = u.stats()
    assert st["instruments"] == len(u.rows)
    assert st["spot"] + st["futures"] + st["inverse"] == st["instruments"]
    assert sum(v["total"] for v in st["venues"]) == st["instruments"]
    assert st["source"] == "offline"
    assert {v["venue"] for v in st["venues"]} == set(VENUES)
    assert st["quotes"][0]["quote"] == "USDT"


def test_find_and_csv():
    u = build()
    rows = u.find("BTC/USDT")
    assert rows and all(r["symbol"] == "BTC/USDT" for r in rows)
    assert u.find("btc-usdt", venue="binance") == [r for r in rows if r["venue"] == "binance"]
    csv = u.to_csv(rows)
    lines = csv.splitlines()
    assert lines[0].startswith("venue,market,symbol")
    assert len(lines) == len(rows) + 1


def test_refresh_falls_back_to_offline_when_venues_unreachable():
    u = Universe()
    report = asyncio.run(u.refresh())
    assert report["count"] == len(u.rows) > 0
    assert report["source"] in ("rest", "offline")
    assert not u.stale
    # a second call inside the TTL is a no-op
    rows_id = id(u.rows)
    asyncio.run(u.refresh())
    assert id(u.rows) == rows_id


def test_scoped_refresh_keeps_other_partitions():
    u = build()
    total = len(u.rows)
    asyncio.run(u.refresh(["okx"], ["spot"], force=True))
    assert len(u.rows) == total, "a scoped refresh must not wipe the other venues"
    assert {r["venue"] for r in u.rows} == set(VENUES)
    assert len(u.report["ok"]) >= 1


def test_offline_report_lists_every_partition():
    u = Universe()
    report = asyncio.run(u.refresh())
    if report["source"] == "offline":
        partitions = {(r["venue"], r["market"]) for r in u.rows}
        assert len(report["ok"]) == len(partitions)
        assert sum(o["count"] for o in report["ok"]) == len(u.rows)
        assert all(o.get("offline") for o in report["ok"])


def test_presets_are_valid_and_applied():
    from app.universe import PRESETS, SORTS

    u = build()
    ids = [p["id"] for p in PRESETS]
    assert len(ids) == len(set(ids))
    for p in PRESETS:
        assert p["label"] and p["desc"]
        sort = p["params"].get("sort", "volume")
        assert sort in SORTS, p["id"]
        res = u.query(preset=p["id"], limit=25)
        assert res["preset"] == p["id"]
        for r in res["rows"]:
            if "market" in p["params"]:
                assert r["market"] == p["params"]["market"]
            if "quote" in p["params"]:
                assert r["quote"] == p["params"]["quote"]
            if p["params"].get("min_volume"):
                assert r["volume_usd"] >= p["params"]["min_volume"]
            if p["params"].get("max_volume"):
                assert r["volume_usd"] <= p["params"]["max_volume"]
            if p["params"].get("funding_max") is not None:
                assert r["funding_rate"] is not None
                assert r["funding_rate"] <= p["params"]["funding_max"]


def test_numeric_range_filters():
    u = build()
    up = u.query(change_min=3.0, limit=500)
    assert up["rows"] and all(r["change_pct"] >= 3.0 for r in up["rows"])
    band = u.query(change_min=-1.0, change_max=1.0, limit=500)
    assert all(-1.0 <= r["change_pct"] <= 1.0 for r in band["rows"])
    paying = u.query(funding_min=0.0002, limit=500)
    assert paying["rows"] and all(r["funding_rate"] >= 0.0002 for r in paying["rows"])
    thin = u.query(max_volume=1e6, limit=500)
    assert all(r["volume_usd"] <= 1e6 for r in thin["rows"])


def test_inverse_market_is_indexed():
    u = build()
    inv = u.query(market="inverse", limit=500)
    assert inv["total"] > 0
    assert all(r["market"] == "inverse" and r["quote"] == "USD" for r in inv["rows"])
    st = u.stats()
    assert st["inverse"] == inv["total"]
    assert any(v["inverse"] > 0 for v in st["venues"])


def test_carry_ranks_funding_plus_basis():
    u = build()
    rows = u.carry(limit=15, min_volume=0)
    assert rows
    for r in rows:
        assert r["spot_venue"] and r["perp_venue"]
        assert r["carry_apr"] == pytest.approx(r["funding_apr"] + r["basis_pct"], abs=1e-2)
    assert [r["carry_apr"] for r in rows] == sorted([r["carry_apr"] for r in rows], reverse=True)


def test_exclusives_are_single_venue_coins():
    u = build()
    rows = u.exclusives(limit=50)
    assert rows
    for r in rows:
        listed = {x["venue"] for x in u.rows if x["base"] == r["base"]}
        assert listed == {r["venue"]}, r["base"]


def test_movers_dedupe_per_coin():
    u = build()
    data = u.movers(limit=10, min_volume=0)
    assert len(data["gainers"]) == 10 and len(data["losers"]) == 10
    assert len({r["base"] for r in data["gainers"]}) == 10
    assert data["gainers"][0]["change_pct"] >= data["gainers"][-1]["change_pct"]
    assert data["losers"][0]["change_pct"] <= data["losers"][-1]["change_pct"]


def test_eight_venues_are_indexed():
    u = build()
    st = u.stats()
    assert len(st["venues"]) == 8
    assert {v["venue"] for v in st["venues"]} == set(VENUES)
    assert st["instruments"] > 3000 and st["coins"] > 250
