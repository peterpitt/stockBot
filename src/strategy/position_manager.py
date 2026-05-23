"""
src/strategy/position_manager.py
─────────────────────────────────
部位追蹤、停損、移動停利管理。
每個部位獨立計算，全部在 asyncio event loop 裡處理。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Coroutine, Optional

from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class Position:
    """單一持倉狀態"""
    position_id: int
    stock_id: str
    stock_name: str
    strategy: str           # "long" | "short"
    entry_price: float
    quantity: int           # 張數
    stop_loss: float        # 停損價
    db_id: int = 0          # DB 的 positions.id
    exit_price: float | None = None
    realized_pnl: float | None = None

    # 移動停利
    trailing_pct: float = 0.015        # 從高點回撤 1.5% 觸發
    _best_price: float = field(default=0.0, repr=False)

    opened_at: datetime = field(default_factory=datetime.now)
    is_closed: bool = False

    def __post_init__(self) -> None:
        self._best_price = self.entry_price

    @property
    def current_pnl(self) -> float:
        """未實現損益（元），不含手續費"""
        if self.strategy == "long":
            return (self._best_price - self.entry_price) * self.quantity * 1000
        else:  # short
            return (self.entry_price - self._best_price) * self.quantity * 1000

    def update_price(self, price: float) -> None:
        """更新最新價格，刷新移動停利高水位"""
        if self.strategy == "long":
            self._best_price = max(self._best_price, price)
        else:
            self._best_price = min(self._best_price, price)

    def should_stop_loss(self, current_price: float) -> bool:
        """是否觸發固定停損"""
        if self.strategy == "long":
            return current_price <= self.stop_loss
        else:
            return current_price >= self.stop_loss

    def should_trailing_stop(self, current_price: float) -> bool:
        """
        是否觸發移動停利。
        做多：從最高點回撤 trailing_pct
        做空：從最低點反彈 trailing_pct
        """
        if self.strategy == "long":
            if self._best_price <= self.entry_price:
                return False  # 尚未獲利，不觸發移動停利
            threshold = self._best_price * (1 - self.trailing_pct)
            return current_price <= threshold
        else:
            if self._best_price >= self.entry_price:
                return False
            threshold = self._best_price * (1 + self.trailing_pct)
            return current_price >= threshold

    def calc_pnl(self, exit_price: float) -> float:
        """計算實現損益（元），含估算手續費與稅"""
        if self.strategy == "long":
            gross = (exit_price - self.entry_price) * self.quantity * 1000
        else:
            gross = (self.entry_price - exit_price) * self.quantity * 1000

        # 台股手續費 0.1425% 雙邊 + 證交稅 0.3%（賣方）
        fee = (self.entry_price + exit_price) * self.quantity * 1000 * 0.001425
        tax = exit_price * self.quantity * 1000 * 0.003
        return gross - fee - tax


class PositionManager:
    """
    當日所有持倉的管理器。

    Usage:
        pm = PositionManager(stop_loss_pct=0.02, max_exposure=500_000)
        pos = pm.open("6271", "同欣電", "short", entry=45.5, qty=1)
        exit_reason = pm.check_exit(pos, current_price=46.2)
    """

    def __init__(
        self,
        stop_loss_pct: float = 0.02,
        trailing_pct: float = 0.015,
        max_position_per_stock: int = 100_000,
        max_total_exposure: int = 500_000,
    ) -> None:
        self.stop_loss_pct = stop_loss_pct
        self.trailing_pct = trailing_pct
        self.max_position_per_stock = max_position_per_stock
        self.max_total_exposure = max_total_exposure

        self._positions: dict[str, Position] = {}   # stock_id → Position
        self._counter: int = 0

    @property
    def open_positions(self) -> list[Position]:
        return [p for p in self._positions.values() if not p.is_closed]

    @property
    def total_exposure(self) -> float:
        return sum(p.entry_price * p.quantity * 1000 for p in self.open_positions)

    def can_open(self, price: float, quantity: int, stock_id: str) -> tuple[bool, str]:
        """檢查是否可以開新倉"""
        if stock_id in self._positions and not self._positions[stock_id].is_closed:
            return False, f"[{stock_id}] 已有未平倉部位"

        cost = price * quantity * 1000
        if cost > self.max_position_per_stock:
            return False, f"單檔部位 {cost:,.0f} 超過上限 {self.max_position_per_stock:,}"

        if self.total_exposure + cost > self.max_total_exposure:
            return False, f"總曝險 {self.total_exposure + cost:,.0f} 超過上限 {self.max_total_exposure:,}"

        return True, "ok"

    def open(
        self,
        stock_id: str,
        stock_name: str,
        strategy: str,
        entry_price: float,
        quantity: int,
        db_id: int = 0,
    ) -> Position:
        """建立新部位"""
        self._counter += 1

        # 停損價計算
        if strategy == "long":
            stop_loss = round(entry_price * (1 - self.stop_loss_pct), 2)
        else:
            stop_loss = round(entry_price * (1 + self.stop_loss_pct), 2)

        pos = Position(
            position_id=self._counter,
            stock_id=stock_id,
            stock_name=stock_name,
            strategy=strategy,
            entry_price=entry_price,
            quantity=quantity,
            stop_loss=stop_loss,
            trailing_pct=self.trailing_pct,
            db_id=db_id,
        )
        self._positions[stock_id] = pos

        logger.info(
            "position_opened",
            stock_id=stock_id,
            strategy=strategy,
            entry=entry_price,
            qty=quantity,
            stop_loss=stop_loss,
        )
        return pos

    def check_exit(
        self, pos: Position, current_price: float
    ) -> Optional[str]:
        """
        檢查是否觸發出場條件。
        回傳出場原因字串，None 表示繼續持有。
        """
        if pos.is_closed:
            return None

        pos.update_price(current_price)

        if pos.should_stop_loss(current_price):
            return "stop_loss"

        if pos.should_trailing_stop(current_price):
            return "trailing_stop"

        return None

    def close(self, stock_id: str, exit_price: float) -> Optional[float]:
        """平倉，回傳實現損益"""
        pos = self._positions.get(stock_id)
        if not pos or pos.is_closed:
            return None

        pnl = pos.calc_pnl(exit_price)
        pos.is_closed = True
        pos.exit_price = exit_price
        pos.realized_pnl = pnl

        logger.info(
            "position_closed",
            stock_id=stock_id,
            strategy=pos.strategy,
            entry=pos.entry_price,
            exit=exit_price,
            qty=pos.quantity,
            pnl=f"{pnl:+.0f}",
        )
        return pnl

    def daily_summary(self) -> dict:
        all_pos = list(self._positions.values())
        closed = [p for p in all_pos if p.is_closed]
        return {
            "total_trades": len(all_pos),
            "closed": len(closed),
            "open": len(all_pos) - len(closed),
            "realized_pnl": sum(p.realized_pnl or 0.0 for p in closed),
        }
