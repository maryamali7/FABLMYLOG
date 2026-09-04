from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

from app.config import ROOT
from app.models import Fill, Position, Side

DB_PATH = ROOT / "data" / "robot.db"


class Store:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS fills (
                id TEXT PRIMARY KEY,
                symbol TEXT,
                side TEXT,
                qty REAL,
                price REAL,
                fee REAL,
                ts REAL,
                strategy TEXT,
                exchange TEXT,
                paper INTEGER,
                pnl REAL,
                reason TEXT
            );
            CREATE TABLE IF NOT EXISTS equity (
                ts REAL,
                equity REAL,
                cash REAL,
                exposure REAL
            );
            CREATE TABLE IF NOT EXISTS events (
                ts REAL,
                kind TEXT,
                payload TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);
            CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity(ts);
            """
        )
        await self._db.commit()

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None
        return self._db

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def add_fill(self, fill: Fill) -> None:
        await self.db.execute(
            """INSERT OR REPLACE INTO fills
               (id,symbol,side,qty,price,fee,ts,strategy,exchange,paper,pnl,reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                fill.id,
                fill.symbol,
                fill.side.value,
                fill.qty,
                fill.price,
                fill.fee,
                fill.ts,
                fill.strategy,
                fill.exchange,
                1 if fill.paper else 0,
                fill.pnl,
                fill.reason,
            ),
        )
        await self.db.commit()

    async def add_equity(self, equity: float, cash: float, exposure: float) -> None:
        await self.db.execute(
            "INSERT INTO equity (ts, equity, cash, exposure) VALUES (?,?,?,?)",
            (time.time(), equity, cash, exposure),
        )
        await self.db.commit()

    async def add_event(self, kind: str, payload: dict[str, Any]) -> None:
        await self.db.execute(
            "INSERT INTO events (ts, kind, payload) VALUES (?,?,?)",
            (time.time(), kind, json.dumps(payload)),
        )
        await self.db.commit()

    async def recent_fills(self, limit: int = 80) -> list[dict[str, Any]]:
        cur = await self.db.execute("SELECT * FROM fills ORDER BY ts DESC LIMIT ?", (limit,))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def equity_series(self, limit: int = 400) -> list[dict[str, Any]]:
        cur = await self.db.execute(
            "SELECT ts, equity, cash, exposure FROM equity ORDER BY ts DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        data = [dict(r) for r in rows]
        data.reverse()
        return data

    async def recent_events(self, limit: int = 60) -> list[dict[str, Any]]:
        cur = await self.db.execute("SELECT ts, kind, payload FROM events ORDER BY ts DESC LIMIT ?", (limit,))
        rows = await cur.fetchall()
        out = []
        for r in rows:
            try:
                payload = json.loads(r["payload"])
            except Exception:
                payload = {"raw": r["payload"]}
            out.append({"ts": r["ts"], "kind": r["kind"], "payload": payload})
        return out


def position_from_dict(d: dict[str, Any]) -> Position:
    return Position(
        symbol=d["symbol"],
        side=Side(d["side"]),
        qty=d["qty"],
        entry=d["entry"],
        stop=d["stop"],
        take=d["take"],
        trail=d.get("trail", 0),
        opened_ts=d["opened_ts"],
        strategy=d["strategy"],
        exchange=d.get("exchange", "paper"),
        unrealized=d.get("unrealized", 0),
        peak=d.get("peak", 0),
    )
