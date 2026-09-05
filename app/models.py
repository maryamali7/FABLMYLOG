from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class SignalKind(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    CLOSE = "close"


@dataclass
class Ticker:
    exchange: str
    symbol: str
    last: float
    bid: float
    ask: float
    volume: float
    ts: float
    high: float = 0.0
    low: float = 0.0
    change_pct: float = 0.0

    @property
    def mid(self) -> float:
        if self.bid and self.ask:
            return (self.bid + self.ask) / 2
        return self.last

    @property
    def spread_bps(self) -> float:
        m = self.mid
        if not m or not self.bid or not self.ask:
            return 0.0
        return ((self.ask - self.bid) / m) * 10_000


@dataclass
class TradeTick:
    exchange: str
    symbol: str
    price: float
    qty: float
    side: str
    ts: float


@dataclass
class BookLevel:
    price: float
    qty: float


@dataclass
class OrderBook:
    exchange: str
    symbol: str
    bids: list[BookLevel] = field(default_factory=list)
    asks: list[BookLevel] = field(default_factory=list)
    ts: float = 0.0


@dataclass
class Candle:
    ts: float
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Signal:
    strategy: str
    symbol: str
    kind: SignalKind
    confidence: float
    price: float
    reason: str
    ts: float
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d


@dataclass
class Position:
    symbol: str
    side: Side
    qty: float
    entry: float
    stop: float
    take: float
    trail: float
    opened_ts: float
    strategy: str
    exchange: str = "paper"
    unrealized: float = 0.0
    peak: float = 0.0
    scaled: bool = False
    atr: float = 0.0
    trail_pct: float = 0.0
    spec_id: str = ""
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value
        return d


@dataclass
class Fill:
    id: str
    symbol: str
    side: Side
    qty: float
    price: float
    fee: float
    ts: float
    strategy: str
    exchange: str
    paper: bool
    pnl: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value
        return d
