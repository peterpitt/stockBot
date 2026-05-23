"""
src/strategy/signal_engine.py
───────────────────────────────
進場 / 出場訊號判斷引擎。
只做判斷，不執行下單（下單在 trader.py 的 OrderExecutor）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from src.strategy.indicators import IndicatorState
from src.utils.logger import get_logger

logger = get_logger(__name__)

EntrySignal = Literal["long", "short", None]


@dataclass
class SignalResult:
    signal: EntrySignal
    reason: str
    price: float       # 建議進場價（通常是 last_price）
    confidence: float  # 0-1，給 OrderExecutor 決定下單量


class SignalEngine:
    """
    進場條件判斷。

    做空（隔日沖分點標的）：
      ① 開盤 5 分鐘量能爆量
      ② 現價跌破 VWAP
      ③ 最新 5 分 K 跌破前根低點（趨勢確認）

    做多（投信連買標的）：
      ① 開盤 5 分鐘量能爆量
      ② 現價站穩 VWAP 之上（price > VWAP × 1.001 容錯）
      ③ 最新 5 分 K 收盤 > 前根高點

    出場條件（在 PositionManager 處理，這裡只做進場）：
      - 停損 / 移動停利 → position_manager.check_exit()
      - 13:25 強制平倉 → trader.py 排程觸發
    """

    # ── 參數 ─────────────────────────────────────────────────────────────────
    MIN_BARS_5M: int = 2          # 至少需要 2 根 5 分 K 才能判斷趨勢
    VWAP_TOLERANCE: float = 0.001  # VWAP 上下 0.1% 容忍帶
    MIN_OPEN_VOLUME: int = 300    # 開盤 5 分鐘最低量能門檻（張）

    def evaluate(
        self,
        state: IndicatorState,
        strategy: str,    # "long" | "short"
        already_traded: bool = False,
    ) -> SignalResult:
        """
        評估當前指標狀態，回傳進場訊號。
        
        Args:
            state:          標的的即時指標狀態
            strategy:       watchlist 預設方向
            already_traded: 今日是否已進場過（每股每日只進場一次）
        """
        price = state.last_price
        vwap  = state.vwap

        # ── 基本守門 ─────────────────────────────────────────────────────────
        if already_traded:
            return SignalResult(None, "already_traded_today", price, 0)

        if price <= 0 or vwap <= 0:
            return SignalResult(None, "no_quote_yet", price, 0)

        if len(state.bars_5m) < self.MIN_BARS_5M:
            return SignalResult(
                None,
                f"need_more_bars(have={len(state.bars_5m)},need={self.MIN_BARS_5M})",
                price, 0,
            )

        latest_bar = list(state.bars_5m)[-1]

        # ── 做空訊號 ─────────────────────────────────────────────────────────
        if strategy == "short":
            return self._eval_short(state, price, vwap, latest_bar)

        # ── 做多訊號 ─────────────────────────────────────────────────────────
        if strategy == "long":
            return self._eval_long(state, price, vwap, latest_bar)

        return SignalResult(None, f"unknown_strategy:{strategy}", price, 0)

    def _eval_short(self, state, price, vwap, latest_bar) -> SignalResult:
        """
        做空三條件：
        1. 開盤量能（opening_surge 或 volume 達門檻）
        2. 現價跌破 VWAP（price < vwap × (1 - tolerance)）
        3. 最新 5 分 K 跌破前根低點
        """
        reasons_fail = []

        # 條件 1：量能
        vol_ok = state.opening_surge or state.open_volume_5m >= self.MIN_OPEN_VOLUME
        if not vol_ok:
            reasons_fail.append(
                f"volume_weak(open5m={state.open_volume_5m},need={self.MIN_OPEN_VOLUME})"
            )

        # 條件 2：跌破 VWAP
        vwap_break = price < vwap * (1 - self.VWAP_TOLERANCE)
        if not vwap_break:
            reasons_fail.append(f"above_vwap(price={price:.2f},vwap={vwap:.2f})")

        # 條件 3：跌破前根低點（趨勢確認）
        prev_low_break = (
            state.prev_bar_low > 0
            and latest_bar.close < state.prev_bar_low
        )
        if not prev_low_break:
            reasons_fail.append(
                f"no_prev_low_break(close={latest_bar.close:.2f},prev_low={state.prev_bar_low:.2f})"
            )

        if reasons_fail:
            return SignalResult(None, "short_fail:" + "|".join(reasons_fail), price, 0)

        # 信心度：量能爆量加分
        confidence = 0.6 + (0.4 if state.opening_surge else 0)
        logger.info(
            "short_signal_triggered",
            stock_id=state.stock_id,
            price=price,
            vwap=round(vwap, 2),
            prev_low=state.prev_bar_low,
            open_volume=state.open_volume_5m,
            confidence=confidence,
        )
        return SignalResult("short", "all_conditions_met", price, confidence)

    def _eval_long(self, state, price, vwap, latest_bar) -> SignalResult:
        """
        做多三條件：
        1. 開盤量能爆量
        2. 現價站穩 VWAP 之上
        3. 最新 5 分 K 突破前根高點
        """
        reasons_fail = []

        # 條件 1：量能
        vol_ok = state.opening_surge or state.open_volume_5m >= self.MIN_OPEN_VOLUME
        if not vol_ok:
            reasons_fail.append(f"volume_weak(open5m={state.open_volume_5m})")

        # 條件 2：站穩 VWAP
        above_vwap = price > vwap * (1 + self.VWAP_TOLERANCE)
        if not above_vwap:
            reasons_fail.append(f"below_vwap(price={price:.2f},vwap={vwap:.2f})")

        # 條件 3：突破前根高點
        prev_high_break = (
            state.prev_bar_high > 0
            and latest_bar.close > state.prev_bar_high
        )
        if not prev_high_break:
            reasons_fail.append(
                f"no_prev_high_break(close={latest_bar.close:.2f},prev_high={state.prev_bar_high:.2f})"
            )

        if reasons_fail:
            return SignalResult(None, "long_fail:" + "|".join(reasons_fail), price, 0)

        confidence = 0.6 + (0.4 if state.opening_surge else 0)
        logger.info(
            "long_signal_triggered",
            stock_id=state.stock_id,
            price=price,
            vwap=round(vwap, 2),
            prev_high=state.prev_bar_high,
            open_volume=state.open_volume_5m,
            confidence=confidence,
        )
        return SignalResult("long", "all_conditions_met", price, confidence)
