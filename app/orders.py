"""Order management: the working-order book behind the trade desk.

The robot has always been able to buy and sell at market. A terminal needs more
than that — resting limits, stops that arm and then fire, stop-limits, trailing
stops, brackets that cancel each other, time in force, reduce-only, post-only.

This module owns the *intent* (the order book) and the matching rules; the
engine owns the money (cash, positions, fills). ``match()`` is a pure function
of the current quote plus the resting orders: it returns fill intents and status
changes, and never touches an account.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("oms")

TYPES = ("market", "limit", "stop", "stop_limit", "trailing_stop")
SIDES = ("buy", "sell")
TIFS = ("gtc", "ioc", "fok", "day")
OPEN_STATES = ("working", "triggered")


def _now() -> float:
    return time.time()


@dataclass
class Order:
    symbol: str
    side: str
    type: str = "market"
    qty: float = 0.0                 # base units (0 => use quote_qty)
    quote_qty: float = 0.0           # spend this much quote instead
    price: float = 0.0               # limit price
    stop_price: float = 0.0          # trigger price
    trail_pct: float = 0.0           # trailing stop distance
    tif: str = "gtc"
    reduce_only: bool = False
    post_only: bool = False
    label: str = ""
    source: str = "manual"
    oco_group: str = ""
    parent_id: str = ""
    expires_ts: float = 0.0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "working"
    filled_qty: float = 0.0
    avg_fill: float = 0.0
    created: float = field(default_factory=_now)
    updated: float = field(default_factory=_now)
    triggered_ts: float = 0.0
    peak: float = 0.0                # trailing-stop high-water mark
    reason: str = ""

    @property
    def open(self) -> bool:
        return self.status in OPEN_STATES

    @property
    def remaining(self) -> float:
        return max(0.0, self.qty - self.filled_qty)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["remaining"] = round(self.remaining, 10)
        d["open"] = self.open
        return d


class OrderError(ValueError):
    """Rejected before it ever reaches the book."""


class OMS:
    """Working-order book with a deterministic paper matcher."""

    def __init__(self, path: Path | None = None, max_history: int = 400):
        self.path = path
        self.orders: dict[str, Order] = {}
        self.history: list[dict[str, Any]] = []
        self.max_history = max_history
        self.load()

    # ------------------------------------------------------------------ io
    def load(self) -> None:
        if not self.path:
            return
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except Exception:
            return
        for row in raw.get("orders") or []:
            try:
                order = Order(**{k: v for k, v in row.items() if k in Order.__dataclass_fields__})
                if order.open:
                    self.orders[order.id] = order
            except Exception:
                continue
        self.history = (raw.get("history") or [])[: self.max_history]

    def save(self) -> None:
        if not self.path:
            return
        payload = {
            "orders": [o.to_dict() for o in self.orders.values() if o.open],
            "history": self.history[: self.max_history],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), "utf-8")
            os.replace(tmp, self.path)
        except Exception as exc:  # pragma: no cover - disk only
            log.warning("could not persist orders: %s", exc)

    # -------------------------------------------------------------- placing
    def place(self, order: Order, last: float = 0.0, bid: float = 0.0, ask: float = 0.0) -> Order:
        """Validate and rest an order. Raises OrderError on nonsense."""
        order.symbol = (order.symbol or "").upper().replace("-", "/")
        order.side = (order.side or "").lower()
        order.type = (order.type or "market").lower()
        order.tif = (order.tif or "gtc").lower()
        if order.side not in SIDES:
            raise OrderError(f"side must be buy or sell, not {order.side!r}")
        if order.type not in TYPES:
            raise OrderError(f"unknown order type {order.type!r}")
        if order.tif not in TIFS:
            raise OrderError(f"unknown time in force {order.tif!r}")
        if order.qty <= 0 and order.quote_qty <= 0:
            raise OrderError("order needs a quantity")
        if order.qty <= 0 and order.quote_qty > 0:
            ref = order.price or last or ask or bid
            if ref <= 0:
                raise OrderError("cannot convert quote size without a price")
            order.qty = order.quote_qty / ref
        if order.type in ("limit", "stop_limit") and order.price <= 0:
            raise OrderError(f"{order.type} orders need a limit price")
        if order.type in ("stop", "stop_limit") and order.stop_price <= 0:
            raise OrderError(f"{order.type} orders need a stop price")
        if order.type == "trailing_stop":
            if order.trail_pct <= 0:
                raise OrderError("trailing stops need a trail percentage")
            if order.side != "sell":
                raise OrderError("trailing stops are exit orders — sell side only")
            order.peak = last or bid or order.peak
        if order.post_only and order.type == "limit" and last:
            crosses = (order.side == "buy" and order.price >= (ask or last)) or (
                order.side == "sell" and order.price <= (bid or last)
            )
            if crosses:
                raise OrderError("post-only order would cross the book")
        if order.tif == "day" and not order.expires_ts:
            order.expires_ts = _now() + 86_400
        order.updated = _now()
        self.orders[order.id] = order
        self._log(order, "placed")
        self.save()
        return order

    def cancel(self, order_id: str, reason: str = "cancelled") -> Order | None:
        order = self.orders.get(order_id)
        if not order or not order.open:
            return None
        order.status = "cancelled"
        order.reason = reason
        order.updated = _now()
        self._log(order, "cancelled")
        self._retire(order)
        # an entry that never filled leaves nothing to protect
        for child in list(self.orders.values()):
            if child.parent_id == order.id and child.open and not child.filled_qty:
                child.status = "cancelled"
                child.reason = "entry cancelled"
                child.updated = order.updated
                self._log(child, "cancelled")
                self._retire(child)
        self.save()
        return order

    def cancel_all(self, symbol: str = "", side: str = "") -> int:
        n = 0
        for order in list(self.orders.values()):
            if not order.open:
                continue
            if symbol and order.symbol != symbol.upper().replace("-", "/"):
                continue
            if side and order.side != side.lower():
                continue
            self.cancel(order.id, "cancel all")
            n += 1
        return n

    def modify(self, order_id: str, **patch: Any) -> Order:
        order = self.orders.get(order_id)
        if not order or not order.open:
            raise OrderError("order is not working")
        for key in ("price", "stop_price", "qty", "trail_pct", "label"):
            if key in patch and patch[key] is not None:
                setattr(order, key, patch[key] if key == "label" else float(patch[key]))
        if order.qty <= order.filled_qty:
            raise OrderError("quantity must exceed what is already filled")
        order.updated = _now()
        self._log(order, "modified")
        self.save()
        return order

    # -------------------------------------------------------------- brackets
    def attach_bracket(
        self,
        symbol: str,
        qty: float,
        stop_price: float = 0.0,
        take_price: float = 0.0,
        parent_id: str = "",
        label: str = "bracket",
        trail_pct: float = 0.0,
    ) -> list[Order]:
        """Protective OCO pair for a long position: stop-loss + take-profit."""
        group = uuid.uuid4().hex[:8]
        out: list[Order] = []
        if stop_price > 0:
            out.append(
                Order(symbol=symbol, side="sell", type="stop", qty=qty, stop_price=stop_price,
                      reduce_only=True, oco_group=group, parent_id=parent_id,
                      label=f"{label} stop", source="bracket")
            )
        if take_price > 0:
            out.append(
                Order(symbol=symbol, side="sell", type="limit", qty=qty, price=take_price,
                      reduce_only=True, oco_group=group, parent_id=parent_id,
                      label=f"{label} target", source="bracket")
            )
        if trail_pct > 0:
            out.append(
                Order(symbol=symbol, side="sell", type="trailing_stop", qty=qty, trail_pct=trail_pct,
                      reduce_only=True, oco_group=group, parent_id=parent_id,
                      label=f"{label} trail", source="bracket")
            )
        for order in out:
            self.orders[order.id] = order
            self._log(order, "placed")
        self.save()
        return out

    # -------------------------------------------------------------- matching
    def match(
        self,
        symbol: str,
        last: float,
        bid: float = 0.0,
        ask: float = 0.0,
        position_qty: float = 0.0,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        """Advance every resting order for one symbol against the current quote.

        Returns fill intents: ``{order, qty, price, kind}``. Nothing here mutates
        cash or positions — the engine does that and calls :meth:`confirm`.
        """
        now = now or _now()
        bid = bid or last
        ask = ask or last
        intents: list[dict[str, Any]] = []
        for order in sorted(self.orders.values(), key=lambda o: o.created):
            if not order.open or order.symbol != symbol:
                continue
            if order.expires_ts and now >= order.expires_ts:
                order.status = "expired"
                order.updated = now
                self._log(order, "expired")
                self._retire(order)
                continue
            if self._pending_parent(order):
                continue  # a bracket child sleeps until its entry fills
            if order.reduce_only and position_qty <= 0:
                self.cancel(order.id, "reduce-only with no position")
                continue

            # trailing stops ratchet with price before they can trigger
            if order.type == "trailing_stop":
                order.peak = max(order.peak or last, last)
                order.stop_price = order.peak * (1 - order.trail_pct)

            trigger_px = self._trigger(order, last)
            if order.type in ("stop", "stop_limit", "trailing_stop"):
                if order.status == "working":
                    if trigger_px is None:
                        continue
                    order.status = "triggered"
                    order.triggered_ts = now
                    self._log(order, "triggered")
                    if order.type == "stop_limit":
                        continue  # now behaves as a resting limit

            fill_px = self._fillable(order, last, bid, ask)
            if fill_px is None:
                if order.tif in ("ioc", "fok") and order.status in OPEN_STATES:
                    self.cancel(order.id, f"{order.tif.upper()} not immediately fillable")
                continue

            qty = order.remaining
            if order.reduce_only:
                qty = min(qty, position_qty)
            if qty <= 0:
                self.cancel(order.id, "nothing left to reduce")
                continue
            intents.append({"order": order, "qty": qty, "price": fill_px, "kind": order.type})
        return intents

    def _pending_parent(self, order: Order) -> bool:
        """True while this order's entry is still working.

        Protective orders are placed at the same moment as the entry they
        protect, so they exist before the position does. Left to themselves the
        reduce-only sweep below would cancel them a millisecond after birth.
        """
        if not order.parent_id:
            return False
        parent = self.orders.get(order.parent_id)
        return bool(parent and parent.open)

    def _trigger(self, order: Order, last: float) -> float | None:
        if order.type == "stop" or order.type == "stop_limit":
            if order.side == "buy" and last >= order.stop_price:
                return last
            if order.side == "sell" and last <= order.stop_price:
                return last
            return None
        if order.type == "trailing_stop":
            return last if last <= order.stop_price else None
        return last

    def _fillable(self, order: Order, last: float, bid: float, ask: float) -> float | None:
        if order.type == "market":
            return ask if order.side == "buy" else bid
        if order.type == "limit":
            if order.side == "buy" and (ask or last) <= order.price:
                return min(order.price, ask or last)
            if order.side == "sell" and (bid or last) >= order.price:
                return max(order.price, bid or last)
            return None
        if order.type in ("stop", "trailing_stop"):
            if order.status != "triggered":
                return None
            return ask if order.side == "buy" else bid
        if order.type == "stop_limit":
            if order.status != "triggered":
                return None
            if order.side == "buy" and (ask or last) <= order.price:
                return min(order.price, ask or last)
            if order.side == "sell" and (bid or last) >= order.price:
                return max(order.price, bid or last)
            return None
        return None

    def confirm(self, order: Order, qty: float, price: float) -> Order:
        """Record an executed quantity and settle OCO siblings."""
        prev = order.filled_qty
        order.filled_qty = min(order.qty, prev + qty)
        filled = order.filled_qty
        order.avg_fill = ((order.avg_fill * prev) + price * qty) / filled if filled else price
        order.updated = _now()
        if order.remaining <= 1e-12:
            order.status = "filled"
            self._log(order, "filled")
            self._retire(order)
            if order.oco_group:
                for sibling in list(self.orders.values()):
                    if sibling.id != order.id and sibling.oco_group == order.oco_group and sibling.open:
                        self.cancel(sibling.id, "OCO sibling filled")
        else:
            order.status = "working"
            self._log(order, "partial")
        self.save()
        return order

    def _retire(self, order: Order) -> None:
        self.orders.pop(order.id, None)

    def _log(self, order: Order, event: str) -> None:
        self.history.insert(
            0,
            {
                "ts": _now(),
                "event": event,
                "id": order.id,
                "symbol": order.symbol,
                "side": order.side,
                "type": order.type,
                "qty": round(order.qty, 10),
                "filled": round(order.filled_qty, 10),
                "price": order.price or order.avg_fill or order.stop_price,
                "status": order.status,
                "label": order.label,
                "reason": order.reason,
            },
        )
        self.history = self.history[: self.max_history]

    # ---------------------------------------------------------------- views
    def working(self, symbol: str = "") -> list[dict[str, Any]]:
        rows = [
            o.to_dict()
            for o in self.orders.values()
            if o.open and (not symbol or o.symbol == symbol.upper().replace("-", "/"))
        ]
        rows.sort(key=lambda r: -r["created"])
        return rows

    def snapshot(self, symbols: Iterable[str] | None = None) -> dict[str, Any]:
        working = self.working()
        by_symbol: dict[str, int] = {}
        for row in working:
            by_symbol[row["symbol"]] = by_symbol.get(row["symbol"], 0) + 1
        return {
            "working": working,
            "count": len(working),
            "by_symbol": by_symbol,
            "history": self.history[:60],
            "types": list(TYPES),
            "tifs": list(TIFS),
        }
