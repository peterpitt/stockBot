"""
src/api/quote_dispatcher.py
─────────────────────────────
Tick 分發器：將 Shioaji callback 轉為非同步 Queue，
與 IndicatorWorker 搭配實現零阻塞報價接收。

架構：
  Shioaji callback thread
       │  (call_soon_threadsafe，極輕量)
       ▼
  asyncio.Queue  ←── 這裡是 event loop，安全
       │
       ▼
  IndicatorWorker.consume()  ←── 計算 VWAP/K-bar/MA（可能較重）
       │
       ▼
  SignalEngine.evaluate()    ←── 進場判斷
       │
       ▼
  OrderExecutor              ←── 下單（在 trader.py）
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from src.strategy.indicators import IndicatorState
from src.utils.logger import get_logger

logger = get_logger(__name__)

QUEUE_MAX_SIZE = 2000   # 防止 OOM（高流量時丟棄舊 tick）


@dataclass
class TickEvent:
    stock_id: str
    price: float
    volume: int
    ts: datetime


class QuoteDispatcher:
    """
    管理所有標的的 IndicatorState，並從 Queue 消費 tick 更新。

    Usage:
        dispatcher = QuoteDispatcher(stock_ids, on_signal_callback)
        await dispatcher.start()
        # Shioaji callback 呼叫 dispatcher.on_tick(...)
        await dispatcher.stop()
    """

    def __init__(
        self,
        watchlist: list[dict],
        on_signal: Callable,    # async (stock_id, signal, state) → None
    ) -> None:
        self._watchlist = {s["stock_id"]: s for s in watchlist}
        self._on_signal = on_signal
        self._queue: asyncio.Queue[TickEvent] = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)
        self._states: dict[str, IndicatorState] = {
            sid: IndicatorState(stock_id=sid)
            for sid in self._watchlist
        }
        self._already_traded: set[str] = set()
        self._worker_task: Optional[asyncio.Task] = None
        self._running = False

    @property
    def stock_ids(self) -> list[str]:
        return list(self._watchlist.keys())

    def get_state(self, stock_id: str) -> Optional[IndicatorState]:
        return self._states.get(stock_id)

    # ── Shioaji callback（在 event loop thread 執行，必須極輕量）──────────────
    async def on_tick(
        self, stock_id: str, price: float, volume: int, ts: datetime
    ) -> None:
        """由 ShioajiClient 的 callback 呼叫。放進 Queue 後立刻返回。"""
        event = TickEvent(stock_id=stock_id, price=price, volume=volume, ts=ts)
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # Queue 滿了就丟掉最舊的一筆，繼續放新的
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(event)
            except Exception:
                pass

    # ── Worker：從 Queue 消費，計算指標，觸發訊號 ────────────────────────────
    async def start(self) -> None:
        """啟動 worker coroutine"""
        self._running = True
        self._worker_task = asyncio.create_task(self._consume_loop())
        logger.info("dispatcher_started", stocks=self.stock_ids)

    async def stop(self) -> None:
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("dispatcher_stopped")

    async def _consume_loop(self) -> None:
        """
        持續從 Queue 取出 tick，交給 IndicatorState 更新，
        再交給 SignalEngine 判斷。
        """
        from src.strategy.signal_engine import SignalEngine
        engine = SignalEngine()

        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            state = self._states.get(event.stock_id)
            if not state:
                continue

            # ── 更新指標（計算密集，在 event loop 裡，保持輕量）────────────
            state.update_tick(event.price, event.volume, event.ts)

            # ── 訊號判斷 ─────────────────────────────────────────────────────
            watch = self._watchlist[event.stock_id]
            already_traded = event.stock_id in self._already_traded

            result = engine.evaluate(
                state=state,
                strategy=watch["strategy"],
                already_traded=already_traded,
            )

            if result.signal:
                self._already_traded.add(event.stock_id)
                await self._on_signal(event.stock_id, result, state)

            self._queue.task_done()

    def mark_traded(self, stock_id: str) -> None:
        """下單成功後呼叫，防止重複進場"""
        self._already_traded.add(stock_id)

    def reset_day(self) -> None:
        """每日開盤前重置"""
        self._already_traded.clear()
        for state in self._states.values():
            state.reset_day()
        logger.info("dispatcher_day_reset")
