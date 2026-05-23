"""
src/db/database.py
───────────────────
非同步 SQLite 交易紀錄層。
Tables:
  trades      — 每筆下單紀錄
  positions   — 當日持倉快照
  equity_log  — 每分鐘損益曲線
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiosqlite

from src.utils.logger import get_logger

logger = get_logger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date    TEXT    NOT NULL,
    stock_id      TEXT    NOT NULL,
    stock_name    TEXT    NOT NULL,
    action        TEXT    NOT NULL,   -- BUY | SELL
    price         REAL    NOT NULL,
    quantity      INTEGER NOT NULL,   -- 張數
    order_id      TEXT,
    status        TEXT    DEFAULT 'pending',  -- pending|filled|cancelled|error
    strategy      TEXT,               -- long | short
    signal_source TEXT,
    pnl           REAL    DEFAULT 0,
    created_at    TEXT    NOT NULL,
    filled_at     TEXT,
    note          TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date    TEXT    NOT NULL,
    stock_id      TEXT    NOT NULL,
    stock_name    TEXT    NOT NULL,
    strategy      TEXT    NOT NULL,
    entry_price   REAL    NOT NULL,
    entry_qty     INTEGER NOT NULL,
    current_price REAL    DEFAULT 0,
    stop_loss     REAL    NOT NULL,
    status        TEXT    DEFAULT 'open',  -- open | closed
    pnl           REAL    DEFAULT 0,
    opened_at     TEXT    NOT NULL,
    closed_at     TEXT
);

CREATE TABLE IF NOT EXISTS equity_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,
    total_pnl     REAL    NOT NULL,
    open_positions INTEGER NOT NULL
);
"""


class TradingDB:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(DDL)
        await self._conn.commit()
        logger.info("db_connected", path=str(self.db_path))

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    # ── 寫入下單紀錄 ──────────────────────────────────────────────────────────
    async def insert_trade(
        self,
        trade_date: str,
        stock_id: str,
        stock_name: str,
        action: str,
        price: float,
        quantity: int,
        order_id: str = "",
        strategy: str = "",
        signal_source: list[str] | None = None,
        note: str = "",
    ) -> int:
        assert self._conn
        cur = await self._conn.execute(
            """INSERT INTO trades
               (trade_date,stock_id,stock_name,action,price,quantity,
                order_id,strategy,signal_source,created_at,note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trade_date, stock_id, stock_name, action, price, quantity,
                order_id, strategy,
                json.dumps(signal_source or [], ensure_ascii=False),
                datetime.now().isoformat(), note,
            ),
        )
        await self._conn.commit()
        return cur.lastrowid  # type: ignore

    # ── 更新成交狀態 ──────────────────────────────────────────────────────────
    async def update_trade_filled(
        self, trade_id: int, filled_price: float, pnl: float = 0
    ) -> None:
        assert self._conn
        await self._conn.execute(
            "UPDATE trades SET status='filled', price=?, pnl=?, filled_at=? WHERE id=?",
            (filled_price, pnl, datetime.now().isoformat(), trade_id),
        )
        await self._conn.commit()

    # ── 持倉管理 ──────────────────────────────────────────────────────────────
    async def open_position(
        self,
        trade_date: str,
        stock_id: str,
        stock_name: str,
        strategy: str,
        entry_price: float,
        entry_qty: int,
        stop_loss: float,
    ) -> int:
        assert self._conn
        cur = await self._conn.execute(
            """INSERT INTO positions
               (trade_date,stock_id,stock_name,strategy,entry_price,
                entry_qty,stop_loss,opened_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                trade_date, stock_id, stock_name, strategy,
                entry_price, entry_qty, stop_loss,
                datetime.now().isoformat(),
            ),
        )
        await self._conn.commit()
        return cur.lastrowid  # type: ignore

    async def close_position(
        self, position_id: int, exit_price: float, pnl: float
    ) -> None:
        assert self._conn
        await self._conn.execute(
            """UPDATE positions
               SET status='closed', current_price=?, pnl=?, closed_at=?
               WHERE id=?""",
            (exit_price, pnl, datetime.now().isoformat(), position_id),
        )
        await self._conn.commit()

    async def get_open_positions(self, trade_date: str) -> list[dict]:
        assert self._conn
        cur = await self._conn.execute(
            "SELECT * FROM positions WHERE trade_date=? AND status='open'",
            (trade_date,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ── 損益快照 ──────────────────────────────────────────────────────────────
    async def log_equity(self, total_pnl: float, open_positions: int) -> None:
        assert self._conn
        await self._conn.execute(
            "INSERT INTO equity_log (ts, total_pnl, open_positions) VALUES (?,?,?)",
            (datetime.now().isoformat(), total_pnl, open_positions),
        )
        await self._conn.commit()

    async def today_summary(self, trade_date: str) -> dict:
        assert self._conn
        cur = await self._conn.execute(
            """SELECT COUNT(*) as trades,
                      SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) as wins,
                      SUM(pnl) as total_pnl
               FROM trades WHERE trade_date=? AND status='filled'""",
            (trade_date,),
        )
        row = dict(await cur.fetchone())
        return row
