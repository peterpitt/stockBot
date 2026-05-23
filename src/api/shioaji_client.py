"""
src/api/shioaji_client.py
───────────────────────────
Shioaji 連線、重連、契約查詢管理。

重連機制：
  - on_disconnected callback 觸發後，非同步排程重連
  - 指數退避，最多重連 10 次
  - 重連後自動重新訂閱之前的標的
"""
from __future__ import annotations

import asyncio
from typing import Callable, Optional

try:
    import shioaji as sj
    SHIOAJI_AVAILABLE = True
except ImportError:
    sj = None  # type: ignore
    SHIOAJI_AVAILABLE = False
from shioaji import BidAskFOPv1, Exchange, TickFOPv1, TickSTKv1
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config.settings import get_settings
from src.utils.logger import get_logger

logger = get_logger(__name__)


class ShioajiClient:
    """
    Shioaji API 封裝。
    
    Usage:
        client = ShioajiClient()
        await client.connect()
        contract = client.get_contract("6271")
        await client.subscribe_tick(["6271", "3264"], callback=on_tick)
        # ...
        await client.disconnect()
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._api: Optional[sj.Shioaji] = None
        self._subscribed: set[str] = set()
        self._tick_callback: Optional[Callable] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ── 連線 ──────────────────────────────────────────────────────────────────
    async def connect(self) -> None:
        """連線並完成憑證登入"""
        self._settings.require_shioaji()
        self._loop = asyncio.get_event_loop()

        logger.info("shioaji_connecting", env=self._settings.trading_env)

        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type((ConnectionError, TimeoutError, Exception)),
            wait=wait_exponential(multiplier=1, min=3, max=30),
            stop=stop_after_attempt(5),
            reraise=True,
        ):
            with attempt:
                self._api = sj.Shioaji(
                    simulation=(self._settings.trading_env == "simulation")
                )
                accounts = self._api.login(
                    api_key=self._settings.shioaji_api_key,
                    secret_key=self._settings.shioaji_secret_key,
                    fetch_contract=True,
                )
                logger.info("shioaji_logged_in", accounts=str(accounts))

        # 憑證啟用（production 才需要）
        if self._settings.is_production:
            self._api.activate_ca(
                ca_path=str(self._settings.shioaji_ca_path),
                ca_passwd=self._settings.shioaji_ca_passwd,
                person_id=self._settings.shioaji_account_id,
            )
            logger.info("shioaji_ca_activated")

        # 註冊斷線 callback
        self._api.on_disconnected(self._on_disconnected)
        logger.info("shioaji_connected")

    # ── 斷線重連 ──────────────────────────────────────────────────────────────
    def _on_disconnected(self) -> None:
        """Shioaji 內部 thread 呼叫，用 call_soon_threadsafe 轉回 event loop"""
        logger.warning("shioaji_disconnected", msg="排程重連中...")
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._schedule_reconnect)

    def _schedule_reconnect(self) -> None:
        if self._reconnect_task and not self._reconnect_task.done():
            return  # 重連中，不重複
        self._reconnect_task = asyncio.ensure_future(self._reconnect())

    async def _reconnect(self) -> None:
        logger.info("shioaji_reconnect_start")
        prev_subscribed = set(self._subscribed)
        self._subscribed.clear()

        try:
            await self.connect()
            # 重連後重新訂閱
            if prev_subscribed and self._tick_callback:
                await self.subscribe_tick(list(prev_subscribed), self._tick_callback)
            logger.info("shioaji_reconnect_success", resubscribed=list(prev_subscribed))
        except Exception as e:
            logger.error("shioaji_reconnect_failed", error=str(e))

    # ── 契約查詢 ──────────────────────────────────────────────────────────────
    def get_contract(self, stock_id: str) -> Optional[sj.contracts.Contract]:
        """取得股票契約，找不到回傳 None"""
        if not self._api:
            return None
        try:
            contract = self._api.Contracts.Stocks.TSE.get(stock_id) \
                    or self._api.Contracts.Stocks.OTC.get(stock_id)
            if not contract:
                logger.warning("contract_not_found", stock_id=stock_id)
            return contract
        except Exception as e:
            logger.error("contract_error", stock_id=stock_id, error=str(e))
            return None

    # ── Tick 訂閱 ──────────────────────────────────────────────────────────────
    async def subscribe_tick(
        self,
        stock_ids: list[str],
        callback: Callable,
    ) -> None:
        """
        訂閱多個標的的即時 Tick。
        callback(stock_id: str, price: float, volume: int, ts: datetime) 格式。
        """
        if not self._api:
            raise RuntimeError("請先呼叫 connect()")

        self._tick_callback = callback

        for stock_id in stock_ids:
            if stock_id in self._subscribed:
                continue

            contract = self.get_contract(stock_id)
            if not contract:
                continue

            # 建立輕量 callback（不做任何計算，只轉發到 Queue）
            def make_cb(sid: str):
                def _cb(tick: TickSTKv1, _: bool) -> None:
                    # 在 Shioaji 的 callback thread 執行，必須是輕量操作
                    if self._loop and not self._loop.is_closed():
                        self._loop.call_soon_threadsafe(
                            lambda: asyncio.ensure_future(
                                callback(sid, tick.close, tick.volume, tick.datetime)
                            )
                        )
                return _cb

            self._api.quote.subscribe(
                contract,
                quote_type=sj.constant.QuoteType.Tick,
                version=sj.constant.QuoteVersion.v1,
            )
            self._api.quote.set_on_tick_stk_v1_callback(make_cb(stock_id))
            self._subscribed.add(stock_id)
            logger.info("subscribed_tick", stock_id=stock_id)
            await asyncio.sleep(0.1)  # 避免一次性大量訂閱

        logger.info("subscription_complete", count=len(self._subscribed))

    # ── 下單 ──────────────────────────────────────────────────────────────────
    async def place_order(
        self,
        stock_id: str,
        action: str,     # "Buy" | "Sell"
        quantity: int,   # 張數
        price: float,    # 0 = 市價
        order_type: str = "ROD",
    ) -> Optional[sj.order.Trade]:
        """
        下單。simulation 模式下模擬成交，production 模式真實下單。
        回傳 Trade 物件，None 表示失敗。
        """
        if not self._api:
            logger.error("place_order_no_api")
            return None

        contract = self.get_contract(stock_id)
        if not contract:
            logger.error("place_order_no_contract", stock_id=stock_id)
            return None

        price_type = (
            sj.constant.StockPriceType.MKT if price == 0
            else sj.constant.StockPriceType.LMT
        )

        order = self._api.Order(
            price=price,
            quantity=quantity,
            action=sj.constant.Action.Buy if action == "Buy" else sj.constant.Action.Sell,
            price_type=price_type,
            order_type=sj.constant.OrderType[order_type],
            order_lot=sj.constant.StockOrderLot.Common,
            daytrade_short=(action == "Sell"),  # 當沖賣出標記
        )

        try:
            trade = self._api.place_order(contract, order)
            logger.info(
                "order_placed",
                stock_id=stock_id,
                action=action,
                price=price,
                quantity=quantity,
                order_id=trade.order.id,
            )
            return trade
        except Exception as e:
            logger.error("order_failed", stock_id=stock_id, action=action, error=str(e))
            return None

    async def cancel_order(self, trade: sj.order.Trade) -> bool:
        """撤單"""
        if not self._api:
            return False
        try:
            self._api.cancel_order(trade)
            logger.info("order_cancelled", order_id=trade.order.id)
            return True
        except Exception as e:
            logger.error("cancel_failed", order_id=trade.order.id, error=str(e))
            return False

    # ── 帳戶查詢 ──────────────────────────────────────────────────────────────
    async def get_positions(self) -> list:
        """查詢當前持倉"""
        if not self._api:
            return []
        try:
            return self._api.list_positions(self._api.stock_account)
        except Exception as e:
            logger.error("get_positions_error", error=str(e))
            return []

    # ── 斷線 ──────────────────────────────────────────────────────────────────
    async def disconnect(self) -> None:
        if self._api:
            self._api.logout()
            self._api = None
        logger.info("shioaji_disconnected_clean")
