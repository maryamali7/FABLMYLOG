from __future__ import annotations

"""Signed exchange access: connectivity tests and (guarded) order routing.

Two rules this module exists to enforce:

1. **Nothing signs anything unless you armed it.** Orders require live mode,
   a per-venue ``trade_enabled`` flag and an explicit arm switch in the engine.
2. **A key that cannot read a balance never gets to send an order.** The
   connection test is a signed, read-only balance call; the dashboard shows the
   result before you are allowed to arm.

Order routing is implemented for Binance and Bybit spot. Other venues store
credentials and can be connection-tested, but ``market_buy`` will refuse rather
than guess at an untested signing scheme.

None of the signed paths can be exercised from an air-gapped host — they are
written from each venue's published signing spec and are unverified against
live endpoints.
"""

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from app.keys import TRADABLE, KeyStore
from app.symbols import compact

log = logging.getLogger("live")

TIMEOUT = httpx.Timeout(15.0, connect=8.0)

# read-only balance endpoints used by the "test connection" button
TEST_ENDPOINTS = {
    "binance": ("GET", "https://api.binance.com/api/v3/account"),
    "bybit": ("GET", "https://api.bybit.com/v5/account/wallet-balance?accountType=UNIFIED"),
    "okx": ("GET", "https://www.okx.com/api/v5/account/balance"),
    "mexc": ("GET", "https://api.mexc.com/api/v3/account"),
    "kucoin": ("GET", "https://api.kucoin.com/api/v1/accounts"),
    "gate": ("GET", "https://api.gateio.ws/api/v4/spot/accounts"),
    "bitget": ("GET", "https://api.bitget.com/api/v2/spot/account/assets"),
    "htx": ("GET", "https://api.huobi.pro/v1/account/accounts"),
}


class LiveRouter:
    def __init__(self, keys: KeyStore):
        self.keys = keys

    # ------------------------------------------------------------- helpers
    def venue_ready(self, venue: str) -> bool:
        return self.keys.ready(venue)

    def can_trade(self, venue: str) -> tuple[bool, str]:
        if venue not in TRADABLE:
            return False, f"order routing for {venue} is not wired in this build"
        if not self.keys.ready(venue):
            return False, f"no API credentials stored for {venue}"
        if not (self.keys.data.get(venue) or {}).get("trade_enabled"):
            return False, f"trading is not enabled for {venue}"
        return True, "ok"

    # ---------------------------------------------------------- signatures
    def _binance_headers(self, venue: str, params: dict[str, Any]) -> tuple[str, dict[str, str]]:
        """Binance and MEXC share the HMAC-SHA256 query-string scheme."""
        c = self.keys.creds(venue)
        params = {**params, "timestamp": int(time.time() * 1000), "recvWindow": 5000}
        query = urlencode(params)
        sig = hmac.new(c["secret"].encode(), query.encode(), hashlib.sha256).hexdigest()
        return f"{query}&signature={sig}", {"X-MBX-APIKEY": c["key"]}

    def _bybit_headers(self, venue: str, payload: str) -> dict[str, str]:
        c = self.keys.creds(venue)
        ts = str(int(time.time() * 1000))
        window = "5000"
        pre = ts + c["key"] + window + payload
        sig = hmac.new(c["secret"].encode(), pre.encode(), hashlib.sha256).hexdigest()
        return {
            "X-BAPI-API-KEY": c["key"],
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": window,
            "X-BAPI-SIGN": sig,
            "Content-Type": "application/json",
        }

    def _okx_headers(self, venue: str, method: str, path: str, body: str = "") -> dict[str, str]:
        c = self.keys.creds(venue)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000Z"
        pre = ts + method.upper() + path + body
        sig = base64.b64encode(hmac.new(c["secret"].encode(), pre.encode(), hashlib.sha256).digest()).decode()
        return {
            "OK-ACCESS-KEY": c["key"],
            "OK-ACCESS-SIGN": sig,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": c["passphrase"],
            "Content-Type": "application/json",
        }

    def _kucoin_headers(self, venue: str, method: str, path: str, body: str = "") -> dict[str, str]:
        c = self.keys.creds(venue)
        ts = str(int(time.time() * 1000))
        pre = ts + method.upper() + path + body
        sig = base64.b64encode(hmac.new(c["secret"].encode(), pre.encode(), hashlib.sha256).digest()).decode()
        passphrase = base64.b64encode(
            hmac.new(c["secret"].encode(), c["passphrase"].encode(), hashlib.sha256).digest()
        ).decode()
        return {
            "KC-API-KEY": c["key"],
            "KC-API-SIGN": sig,
            "KC-API-TIMESTAMP": ts,
            "KC-API-PASSPHRASE": passphrase,
            "KC-API-KEY-VERSION": "2",
            "Content-Type": "application/json",
        }

    def _gate_headers(self, venue: str, method: str, path: str, query: str = "", body: str = "") -> dict[str, str]:
        c = self.keys.creds(venue)
        ts = str(int(time.time()))
        hashed = hashlib.sha512(body.encode()).hexdigest()
        pre = "\n".join([method.upper(), path, query, hashed, ts])
        sig = hmac.new(c["secret"].encode(), pre.encode(), hashlib.sha512).hexdigest()
        return {"KEY": c["key"], "Timestamp": ts, "SIGN": sig, "Content-Type": "application/json"}

    def _bitget_headers(self, venue: str, method: str, path: str, body: str = "") -> dict[str, str]:
        c = self.keys.creds(venue)
        ts = str(int(time.time() * 1000))
        pre = ts + method.upper() + path + body
        sig = base64.b64encode(hmac.new(c["secret"].encode(), pre.encode(), hashlib.sha256).digest()).decode()
        return {
            "ACCESS-KEY": c["key"],
            "ACCESS-SIGN": sig,
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": c["passphrase"],
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------- testing
    async def test_connection(self, venue: str) -> dict[str, Any]:
        """Signed, read-only balance call. Never places an order."""
        venue = venue.lower()
        if venue not in TEST_ENDPOINTS:
            return {"ok": False, "detail": f"unknown venue {venue}"}
        if not self.keys.ready(venue):
            return {"ok": False, "detail": "no credentials stored"}
        started = time.time()
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                r = await self._signed_get(client, venue)
            ok = r.status_code == 200
            detail = "authenticated" if ok else f"HTTP {r.status_code}: {r.text[:160]}"
            result = {"ok": ok, "detail": detail, "elapsed_ms": round((time.time() - started) * 1000, 1)}
        except Exception as exc:
            result = {
                "ok": False,
                "detail": f"{exc.__class__.__name__}: {exc}"[:200],
                "elapsed_ms": round((time.time() - started) * 1000, 1),
            }
        self.keys.note_test(venue, result["ok"], result["detail"])
        return {**result, "venue": venue}

    async def _signed_get(self, client: httpx.AsyncClient, venue: str) -> httpx.Response:
        method, url = TEST_ENDPOINTS[venue]
        if venue in ("binance", "mexc"):
            query, headers = self._binance_headers(venue, {})
            return await client.get(f"{url}?{query}", headers=headers)
        if venue == "bybit":
            payload = "accountType=UNIFIED"
            return await client.get(url, headers=self._bybit_headers(venue, payload))
        if venue == "okx":
            return await client.get(url, headers=self._okx_headers(venue, "GET", "/api/v5/account/balance"))
        if venue == "kucoin":
            return await client.get(url, headers=self._kucoin_headers(venue, "GET", "/api/v1/accounts"))
        if venue == "gate":
            return await client.get(url, headers=self._gate_headers(venue, "GET", "/api/v4/spot/accounts"))
        if venue == "bitget":
            return await client.get(url, headers=self._bitget_headers(venue, "GET", "/api/v2/spot/account/assets"))
        if venue == "htx":
            c = self.keys.creds("htx")
            params = {
                "AccessKeyId": c["key"],
                "SignatureMethod": "HmacSHA256",
                "SignatureVersion": "2",
                "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            }
            pre = "\n".join(["GET", "api.huobi.pro", "/v1/account/accounts", urlencode(sorted(params.items()))])
            sig = base64.b64encode(hmac.new(c["secret"].encode(), pre.encode(), hashlib.sha256).digest()).decode()
            return await client.get(f"{url}?{urlencode(params)}&Signature={sig}")
        raise RuntimeError(f"no signed test for {venue}")

    # -------------------------------------------------------------- orders
    async def market_buy(self, venue: str, symbol: str, quote_amount: float) -> dict[str, Any]:
        ok, why = self.can_trade(venue)
        if not ok:
            raise RuntimeError(why)
        if venue == "binance":
            return await self._binance_order(symbol, "BUY", quote=quote_amount)
        if venue == "bybit":
            return await self._bybit_order(symbol, "Buy", qty=quote_amount, market_unit="quoteCoin")
        raise RuntimeError(f"live venue {venue} not wired")

    async def market_sell(self, venue: str, symbol: str, qty: float) -> dict[str, Any]:
        ok, why = self.can_trade(venue)
        if not ok:
            raise RuntimeError(why)
        if venue == "binance":
            return await self._binance_order(symbol, "SELL", qty=qty)
        if venue == "bybit":
            return await self._bybit_order(symbol, "Sell", qty=qty, market_unit="baseCoin")
        raise RuntimeError(f"live venue {venue} not wired")

    async def _binance_order(
        self, symbol: str, side: str, quote: float | None = None, qty: float | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"symbol": compact(symbol), "side": side, "type": "MARKET"}
        if quote is not None:
            params["quoteOrderQty"] = f"{quote:.8f}"
        elif qty is not None:
            params["quantity"] = f"{qty:.8f}"
        query, headers = self._binance_headers("binance", params)
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(f"https://api.binance.com/api/v3/order?{query}", headers=headers)
            r.raise_for_status()
            return r.json()

    async def _bybit_order(self, symbol: str, side: str, qty: float, market_unit: str) -> dict[str, Any]:
        body = json.dumps(
            {
                "category": "spot",
                "symbol": compact(symbol),
                "side": side,
                "orderType": "Market",
                "qty": f"{qty:.8f}",
                "marketUnit": market_unit,
            },
            separators=(",", ":"),
        )
        headers = self._bybit_headers("bybit", body)
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post("https://api.bybit.com/v5/order/create", headers=headers, content=body)
            r.raise_for_status()
            data = r.json()
            if str(data.get("retCode")) != "0":
                raise RuntimeError(f"bybit rejected the order: {data.get('retMsg')}")
            return data
