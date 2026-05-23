"""
src/strategy/indicators.py
────────────────────────────
即時指標計算：VWAP、K 線合成、移動平均。

設計原則：
- 所有計算純 Python + numpy，無鎖、無 I/O，可在 worker thread 安全呼叫
- 每個 stock 獨立一個 IndicatorState，不共享狀態
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np


@dataclass
class KBar:
    """單根 K 棒"""
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int   # 張數

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open

    @property
    def body_size(self) -> float:
        return abs(self.close - self.open)


@dataclass
class IndicatorState:
    """
    單一標的的即時指標狀態。
    由 IndicatorWorker 從 Queue 消費 Tick 後持續更新。
    """
    stock_id: str
    period_1m: int = 1      # 1 分鐘 K
    period_5m: int = 5      # 5 分鐘 K
    ma_period: int = 21     # MA 週期（5 分 K）

    # ── VWAP 累計器（當日重置）──────────────────────────────────────────────
    _vwap_cum_pv: float = field(default=0.0, repr=False)   # Σ(price × volume)
    _vwap_cum_v: int = field(default=0, repr=False)        # Σvolume

    # ── Tick 暫存（合成 K 棒用）────────────────────────────────────────────
    _tick_buf_1m: list[tuple[float, int]] = field(default_factory=list, repr=False)
    _tick_buf_5m: list[tuple[float, int]] = field(default_factory=list, repr=False)
    _bar_start_1m: Optional[datetime] = field(default=None, repr=False)
    _bar_start_5m: Optional[datetime] = field(default=None, repr=False)

    # ── 已完成的 K 棒（FIFO，最多保留 60 根）──────────────────────────────
    bars_1m: deque[KBar] = field(default_factory=lambda: deque(maxlen=60), repr=False)
    bars_5m: deque[KBar] = field(default_factory=lambda: deque(maxlen=60), repr=False)

    # ── 最新報價 ────────────────────────────────────────────────────────────
    last_price: float = 0.0
    last_volume: int = 0
    open_price: float = 0.0        # 開盤價（第一筆 tick）
    open_volume_5m: int = 0        # 開盤前 5 分鐘累計量

    # ── 衍生指標（每次更新後刷新）──────────────────────────────────────────
    vwap: float = 0.0
    ma21: float = 0.0              # 5 分 K 的 21MA
    prev_bar_low: float = 0.0      # 前根 5 分 K 的最低價（做空進場參考）
    prev_bar_high: float = 0.0     # 前根 5 分 K 的最高價（做多進場參考）

    # ── 開盤爆量標記 ────────────────────────────────────────────────────────
    opening_surge: bool = False    # 開盤 5 分鐘量能是否爆量

    def update_tick(self, price: float, volume: int, ts: datetime) -> None:
        """
        消費一筆 Tick，更新所有指標。
        此函式在 worker thread 執行，不可有任何 await。
        """
        self.last_price = price
        self.last_volume = volume

        # 記錄開盤價
        if self.open_price == 0.0:
            self.open_price = price

        # ── VWAP ─────────────────────────────────────────────────────────────
        self._vwap_cum_pv += price * volume
        self._vwap_cum_v += volume
        if self._vwap_cum_v > 0:
            self.vwap = self._vwap_cum_pv / self._vwap_cum_v

        # ── 合成 1 分鐘 K ────────────────────────────────────────────────────
        self._aggregate_bar(price, volume, ts, minutes=1)

        # ── 合成 5 分鐘 K ────────────────────────────────────────────────────
        self._aggregate_bar(price, volume, ts, minutes=5)

        # ── 更新 MA21（5 分 K）──────────────────────────────────────────────
        if len(self.bars_5m) >= self.ma_period:
            closes = np.array([b.close for b in list(self.bars_5m)[-self.ma_period:]])
            self.ma21 = float(np.mean(closes))

        # ── 前根 K 棒資訊 ────────────────────────────────────────────────────
        if len(self.bars_5m) >= 2:
            prev = list(self.bars_5m)[-2]
            self.prev_bar_low = prev.low
            self.prev_bar_high = prev.high

    def _aggregate_bar(
        self, price: float, volume: int, ts: datetime, minutes: int
    ) -> None:
        """將 tick 聚合進對應週期的 K 棒。"""
        buf = self._tick_buf_1m if minutes == 1 else self._tick_buf_5m
        bars = self.bars_1m if minutes == 1 else self.bars_5m

        # 取當前分鐘的「週期起始時間」
        bar_start = ts.replace(
            second=0, microsecond=0,
            minute=(ts.minute // minutes) * minutes,
        )

        prev_start = self._bar_start_1m if minutes == 1 else self._bar_start_5m

        # 新週期開始 → 把前一根 buf 封存成 KBar
        if prev_start is not None and bar_start != prev_start and buf:
            prices = [p for p, _ in buf]
            vols   = [v for _, v in buf]
            completed = KBar(
                ts=prev_start,
                open=prices[0],
                high=max(prices),
                low=min(prices),
                close=prices[-1],
                volume=sum(vols),
            )
            bars.append(completed)

            # 開盤 5 分鐘爆量判定（只做一次）
            if minutes == 5 and len(bars) == 1:
                self.open_volume_5m = completed.volume
                # 超過前 5 日日均量的 2 倍視為爆量（此處用簡單門檻 500 張）
                self.opening_surge = completed.volume >= 500

            buf.clear()

        buf.append((price, volume))
        if minutes == 1:
            self._bar_start_1m = bar_start
        else:
            self._bar_start_5m = bar_start

    def reset_day(self) -> None:
        """每日開盤前重置（保留設定）"""
        self._vwap_cum_pv = 0.0
        self._vwap_cum_v = 0
        self._tick_buf_1m.clear()
        self._tick_buf_5m.clear()
        self._bar_start_1m = None
        self._bar_start_5m = None
        self.bars_1m.clear()
        self.bars_5m.clear()
        self.last_price = 0.0
        self.open_price = 0.0
        self.open_volume_5m = 0
        self.vwap = 0.0
        self.ma21 = 0.0
        self.prev_bar_low = 0.0
        self.prev_bar_high = 0.0
        self.opening_surge = False
