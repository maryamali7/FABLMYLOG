from __future__ import annotations

"""Optional live order routing. Disabled unless BOT_MODE=live and keys exist.

Only signed REST is used — never sends orders in paper mode.
"""

import hashlib
import hmac
import logging
import time
from urllib.parse import urlencode

import httpx

from app.config import api_keys
from app.symbols import compact

log = logging.getLogger("live")


class LiveRouter:
    def __init__(self):
        self.keys = api_keys()

    def venue_ready(self, name: str) -> bool:
        k = self.keys.get(name) or {}
        return bool(k.get("key") and k.get("secret"))

    async def market_buy(self, venue: str, symbol: str, quote_amount: float) -> dict:
        if venue == "binance":
            return await self._binance_market(symbol, side="BUY", quote=quote_amount)
        raise RuntimeError(f"live venue {venue} not wired")

    async def market_sell(self, venue: str, symbol: str, qty: float) -> dict:
        if venue == "binance":
            return await self._binance_market(symbol, side="SELL", qty=qty)
        raise RuntimeError(f"live venue {venue} not wired")

    async def _binance_market(
        self, symbol: str, side: str, quote: float | None = None, qty: float | None = None
    ) -> dict:
        k = self.keys["binance"]
        params: dict[str, str | int | float] = {
            "symbol": compact(symbol),
            "side": side,
            "type": "MARKET",
            "timestamp": int(time.time() * 1000),
            "recvWindow": 5000,
        }
        if side == "BUY" and quote is not None:
            params["quoteOrderQty"] = f"{quote:.8f}"
        elif qty is not None:
            params["quantity"] = f"{qty:.8f}"
        query = urlencode(params)
        sig = hmac.new(k["secret"].encode(), query.encode(), hashlib.sha256).hexdigest()
        url = f"https://api.binance.com/api/v3/order?{query}&signature={sig}"
        headers = {"X-MBX-APIKEY": k["key"]}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, headers=headers)
            r.raise_for_status()
            return r.json()
