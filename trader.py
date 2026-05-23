"""
trader.py — 台股單檔當沖主程式（修訂版）

策略規則：
  - 每日只交易一檔（從 watchlist selected 開始，漲停換備用）
  - 價位篩選：10~70 元（開盤確認，超出範圍跳過）
  - 開盤漲停 or 10:00 前漲停 → 跳下一檔備用
  - 進場後停損 -2%、移動停利 +1.5%
  - 13:25 強制市價平倉

執行：
  python trader.py --dry-run       # 模擬（注入 mock tick，完整走完流程）
  python trader.py                 # 真實（需 .env 憑證）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

# ── 確保 stdout 與 stderr 使用 UTF-8 編碼，避免在 Windows/非 UTF-8 終端下輸出 Emoji 或中文時崩潰 ──
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
if hasattr(sys.stderr, 'reconfigure'):
    try:
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

from config.settings import get_settings
from src.api.quote_dispatcher import QuoteDispatcher
try:
    from src.api.shioaji_client import ShioajiClient
except ImportError:
    ShioajiClient = None  # type: ignore
from src.db.database import TradingDB
from src.strategy.indicators import IndicatorState, KBar
from src.strategy.position_manager import PositionManager
from src.strategy.signal_engine import SignalEngine, SignalResult
from src.utils.logger import get_logger, setup_logging
from src.utils.time_utils import TW_TZ, now_tw, trading_date

settings = get_settings()
setup_logging(settings.log_level, settings.log_dir)
logger = get_logger("trader")

PRICE_MIN = 10.0
PRICE_MAX = 70.0
LIMIT_UP_PCT = 0.10        # 漲停 +10%
ABANDON_LIMIT_UP_BY = "10:00"  # 10:00 前還在漲停就放棄


# ══════════════════════════════════════════════════════════════════════════════
#  漲停偵測
# ══════════════════════════════════════════════════════════════════════════════
def is_limit_up(price: float, ref_price: float) -> bool:
    """是否達漲停（ref_price = 昨日收盤，若無則用開盤價）"""
    if ref_price <= 0:
        return False
    return price >= ref_price * (1 + LIMIT_UP_PCT - 0.001)  # 容錯 0.1%


# ══════════════════════════════════════════════════════════════════════════════
#  OrderExecutor（單檔版）
# ══════════════════════════════════════════════════════════════════════════════
class OrderExecutor:
    def __init__(self, client: ShioajiClient, pm: PositionManager,
                 db: TradingDB, dry_run: bool = False, trade_date: str | None = None) -> None:
        self.client = client
        self.pm = pm
        self.db = db
        self.dry_run = dry_run
        self.trade_date = trade_date or trading_date()

    async def enter(self, stock: dict, price: float, signal: str) -> bool:
        """
        進場下單。回傳 True 表示成功建倉。
        quantity 固定 1 張（可依 max_position_per_stock / price 動態調整）
        """
        quantity = max(1, int(settings.max_position_per_stock / (price * 1000)))
        can, reason = self.pm.can_open(price, quantity, stock["stock_id"])
        if not can:
            logger.warning("entry_blocked", stock_id=stock["stock_id"], reason=reason)
            return False

        action = "Sell" if signal == "short" else "Buy"
        logger.info("entering", stock_id=stock["stock_id"], signal=signal,
                    price=price, qty=quantity, dry_run=self.dry_run)

        order_id = "DRY_RUN"
        if not self.dry_run:
            trade = await self.client.place_order(
                stock_id=stock["stock_id"],
                action=action,
                quantity=quantity,
                price=round(price * (0.998 if signal == "short" else 1.002), 2),
            )
            if not trade:
                return False
            order_id = trade.order.id

        db_pid = await self.db.open_position(
            trade_date=self.trade_date,
            stock_id=stock["stock_id"],
            stock_name=stock["stock_name"],
            strategy=signal,
            entry_price=price,
            entry_qty=quantity,
            stop_loss=(price * (1 + settings.stop_loss_pct) if signal == "short"
                       else price * (1 - settings.stop_loss_pct)),
        )
        self.pm.open(stock["stock_id"], stock["stock_name"],
                     signal, price, quantity, db_id=db_pid)

        await self.db.insert_trade(
            trade_date=self.trade_date,
            stock_id=stock["stock_id"], stock_name=stock["stock_name"],
            action=action, price=price, quantity=quantity,
            order_id=order_id, strategy=signal,
            signal_source=stock.get("signal_source", []),
        )
        return True

    async def exit(self, stock_id: str, price: float, reason: str) -> None:
        """平倉下單 + 紀錄"""
        pos_list = [p for p in self.pm.open_positions if p.stock_id == stock_id]
        if not pos_list:
            return
        pos = pos_list[0]

        if not self.dry_run:
            close_action = "Buy" if pos.strategy == "short" else "Sell"
            await self.client.place_order(stock_id, close_action, pos.quantity, price=0)

        pnl = self.pm.close(stock_id, price)
        if pnl is not None and pos.db_id:
            await self.db.close_position(pos.db_id, price, pnl)

        logger.info("exited", stock_id=stock_id, price=price,
                    reason=reason, pnl=f"{pnl:+.0f}" if pnl else "N/A")


# ══════════════════════════════════════════════════════════════════════════════
#  DayTrader（單檔策略主控）
# ══════════════════════════════════════════════════════════════════════════════
class DayTrader:
    def __init__(self, dry_run: bool = False, custom_date: str | None = None) -> None:
        self.dry_run = dry_run
        self.custom_date = custom_date
        self.client = ShioajiClient() if ShioajiClient else None
        self.db = TradingDB(settings.sqlite_path)
        self.pm = PositionManager(
            stop_loss_pct=settings.stop_loss_pct,
            max_position_per_stock=settings.max_position_per_stock,
            max_total_exposure=settings.max_total_exposure,
        )
        self.engine = SignalEngine()

        # 當日狀態
        self._active_stock: dict | None = None   # 當前主力標的
        self._backup: list[dict] = []            # 備用清單
        self._state: IndicatorState | None = None
        self._entered = False                    # 今日是否已進場
        self._abandoned = False                  # 今日是否已放棄
        self._ref_price: float = 0.0             # 參考價（昨收或開盤）
        self._executor: OrderExecutor | None = None

    # ── 載入 watchlist ────────────────────────────────────────────────────────
    def _load_watchlist(self) -> tuple[dict | None, list[dict]]:
        wl_path = settings.watchlist_path
        if not wl_path.exists():
            raise FileNotFoundError(f"找不到 watchlist：{wl_path}\n請先執行 scanner")

        with open(wl_path, encoding="utf-8") as f:
            data = json.load(f)

        selected = data.get("selected")
        backup = data.get("backup", [])

        logger.info("watchlist_loaded",
                    selected=selected["stock_id"] if selected else None,
                    backup=[s["stock_id"] for s in backup])
        return selected, backup

    # ── 換股（漲停時呼叫）────────────────────────────────────────────────────
    def _switch_to_next(self, reason: str) -> bool:
        """
        從 backup 取下一檔。回傳 True 表示成功換股，False 表示備用已耗盡。
        """
        if not self._backup:
            logger.warning("no_backup_remaining", reason=reason)
            self._abandoned = True
            return False

        next_stock = self._backup.pop(0)
        logger.info("switching_stock",
                    from_id=self._active_stock["stock_id"] if self._active_stock else "None",
                    to_id=next_stock["stock_id"],
                    reason=reason)
        self._active_stock = next_stock
        self._state = IndicatorState(stock_id=next_stock["stock_id"])
        self._ref_price = 0.0
        return True

    # ── 開盤確認邏輯 ──────────────────────────────────────────────────────────
    def _check_open_conditions(self, price: float) -> tuple[bool, str]:
        """
        開盤後確認是否適合交易本檔。
        回傳 (ok, reason)
        """
        # 價位不在 10~70 元
        if not (PRICE_MIN <= price <= PRICE_MAX):
            return False, f"price_out_of_range({price:.2f},need {PRICE_MIN}~{PRICE_MAX})"

        # 開盤即漲停
        if self._ref_price > 0 and is_limit_up(price, self._ref_price):
            return False, f"limit_up_at_open(price={price:.2f},ref={self._ref_price:.2f})"

        return True, "ok"

    # ── 主流程 ────────────────────────────────────────────────────────────────
    async def start(self) -> None:
        logger.info("trader_start", dry_run=self.dry_run, env=settings.trading_env)

        selected, backup = self._load_watchlist()
        if not selected:
            logger.warning("no_selected_stock")
            return

        self._active_stock = selected
        self._backup = backup
        self._state = IndicatorState(stock_id=selected["stock_id"])

        await self.db.connect()
        self._executor = OrderExecutor(self.client, self.pm, self.db, self.dry_run, self.custom_date)

        if not self.dry_run:
            await self.client.connect()

        logger.info("trader_ready",
                    active=self._active_stock["stock_id"],
                    strategy=self._active_stock["strategy"],
                    backup=[s["stock_id"] for s in self._backup])

        if self.dry_run:
            await self._run_dry_run()
        else:
            await self._run_live()

    async def _run_live(self) -> None:
        """真實盤中：訂閱 Tick，用 dispatcher 驅動"""
        # 訂閱當前主力標的
        async def on_tick(sid, price, volume, ts):
            await self._process_tick(sid, price, volume, ts)

        all_ids = ([self._active_stock["stock_id"]] +
                   [s["stock_id"] for s in self._backup])
        await self.client.subscribe_tick(all_ids, on_tick)

        # 監控迴圈
        while True:
            await asyncio.sleep(1)
            now = now_tw()

            if self._abandoned:
                logger.info("day_abandoned", msg="今日無適合標的，等待收盤")
                break

            # 13:25 強制平倉
            if now.strftime("%H:%M") >= "13:25":
                await self._force_close()
                break

            # 持倉中：檢查停損/停利
            if self._entered and self._state:
                await self._check_exits(self._state.last_price)

        await self.stop()

    async def _process_tick(self, sid: str, price: float, volume: int, ts: datetime) -> None:
        """接收 Tick 並驅動狀態機"""
        # 只處理當前主力標的的 tick
        if not self._active_stock or sid != self._active_stock["stock_id"]:
            return
        if self._abandoned or self._entered:
            # 已進場時仍需更新指標（供停損判斷），但不再找進場點
            if self._state:
                self._state.update_tick(price, volume, ts)
            return

        state = self._state
        state.update_tick(price, volume, ts)

        # ── 開盤確認（第一筆 tick）────────────────────────────────────────
        if state.open_price > 0 and self._ref_price == 0:
            self._ref_price = state.open_price
            ok, reason = self._check_open_conditions(state.open_price)
            if not ok:
                logger.warning("open_condition_fail",
                               stock_id=sid, reason=reason)
                if not self._switch_to_next(reason):
                    return
                # 換股後重新訂閱（live 模式）
                return

        # ── 10:00 前漲停偵測 ──────────────────────────────────────────────
        if ts.strftime("%H:%M") < ABANDON_LIMIT_UP_BY:
            if is_limit_up(price, self._ref_price):
                reason = f"limit_up_before_{ABANDON_LIMIT_UP_BY}"
                logger.warning("limit_up_skip", stock_id=sid, price=price)
                if not self._switch_to_next(reason):
                    return
                return

        # ── 進場訊號 ──────────────────────────────────────────────────────
        result = self.engine.evaluate(
            state=state,
            strategy=self._active_stock["strategy"],
            already_traded=self._entered,
        )
        if result.signal:
            ok = await self._executor.enter(self._active_stock, price, result.signal)
            if ok:
                self._entered = True

    async def _check_exits(self, price: float) -> None:
        if not self._active_stock or not self._entered:
            return
        pos_list = [p for p in self.pm.open_positions
                    if p.stock_id == self._active_stock["stock_id"]]
        if not pos_list:
            return
        pos = pos_list[0]
        reason = self.pm.check_exit(pos, price)
        if reason:
            await self._executor.exit(self._active_stock["stock_id"], price, reason)
            self._entered = False

    async def _force_close(self) -> None:
        if not self._active_stock:
            return
        open_pos = self.pm.open_positions
        if not open_pos:
            logger.info("force_close_no_positions")
            return
        logger.warning("force_close_13:25", count=len(open_pos))
        for pos in open_pos:
            price = (self._state.last_price if self._state and self._state.last_price > 0
                     else pos.entry_price)
            await self._executor.exit(pos.stock_id, price, "force_close_13:25")

    # ── DRY RUN：完整模擬整個盤中流程 ─────────────────────────────────────────
    async def _run_dry_run(self) -> None:
        """
        注入完整模擬 tick 序列，真實走過所有策略邏輯：
        09:00 開盤 → 第一根 5 分 K 完成 → 第二根 K 跌破前低 → 進場做空
        → 停損 or 移動停利 → 13:25 強制平倉
        """
        stock = self._active_stock
        strategy = stock["strategy"]
        sid = stock["stock_id"]

        logger.info("dry_run_start", stock_id=sid, strategy=strategy)
        print(f"\n{'='*55}")
        print(f"  DRY RUN — [{sid}] {stock['stock_name']}  策略:{strategy.upper()}")
        print(f"{'='*55}")

        # 模擬參考價（昨收 45 元，在 10~70 範圍內）
        self._ref_price = 45.0

        # 基準時間：今日 09:00
        base = now_tw().replace(hour=9, minute=0, second=0, microsecond=0)

        # ── 模擬 tick 序列設計 ──────────────────────────────────────────
        # 做空情境：
        #   9:00~9:05  開盤爆量，價格在 45.5，高於昨收不漲停
        #   9:05~9:10  K1 完成（open=45.5, high=46.0, low=45.2, close=45.3）
        #   9:10~9:15  K2 形成中，跌破 K1 低點 45.2 → 觸發進場
        #   進場後 → 價格繼續跌 → 移動停利出場
        #
        # 做多情境（長的方向相反）

        ticks: list[tuple[float, int, datetime]] = []

        if strategy == "short":
            # K1: 9:00~9:05，量大（爆量），價格稍跌
            for i in range(10):
                t = base + timedelta(seconds=i * 30)
                p = 45.5 - i * 0.02          # 緩跌
                v = 80 if i < 5 else 60       # 開盤量大
                ticks.append((round(p, 2), v, t))

            # K2 起始：9:05~，跌破 K1 低點
            k1_low = min(p for p, _, t in ticks if t < base + timedelta(minutes=5))
            for i in range(10):
                t = base + timedelta(minutes=5, seconds=i * 30)
                p = k1_low - 0.1 - i * 0.05  # 跌破前低
                v = 50
                ticks.append((round(p, 2), v, t))

            # K3: 繼續跌（觸發移動停利）
            entry_est = k1_low - 0.2          # 估計進場價
            trailing_target = entry_est * (1 - 0.025)  # 跌 2.5% 後反彈
            for i in range(6):
                t = base + timedelta(minutes=10, seconds=i * 30)
                p = entry_est - 0.05 - i * 0.08
                v = 40
                ticks.append((round(p, 2), v, t))

            # 反彈觸發移動停利
            best = min(p for p, _, _ in ticks[-6:])
            for i in range(4):
                t = base + timedelta(minutes=12, seconds=i * 30)
                p = best + (i + 1) * 0.15    # 從低點反彈
                v = 30
                ticks.append((round(p, 2), v, t))

        else:  # long
            for i in range(10):
                t = base + timedelta(seconds=i * 30)
                p = 45.5 + i * 0.02
                v = 80 if i < 5 else 60
                ticks.append((round(p, 2), v, t))

            k1_high = max(p for p, _, _ in ticks)
            for i in range(10):
                t = base + timedelta(minutes=5, seconds=i * 30)
                p = k1_high + 0.1 + i * 0.05
                v = 50
                ticks.append((round(p, 2), v, t))

            for i in range(6):
                t = base + timedelta(minutes=10, seconds=i * 30)
                p = k1_high + 0.3 + i * 0.08
                v = 40
                ticks.append((round(p, 2), v, t))

        # ── 注入 tick，即時打印狀態 ────────────────────────────────────────
        print(f"\n  開始注入 {len(ticks)} 筆模擬 tick...\n")

        for price, volume, ts in ticks:
            self._state.update_tick(price, volume, ts)
            state = self._state

            # 開盤確認（僅第一筆）
            if state.open_price > 0 and self._ref_price == 0:
                self._ref_price = state.open_price
                ok, reason = self._check_open_conditions(state.open_price)
                if not ok:
                    print(f"  [SKIP] 開盤條件不符：{reason}")
                    self._switch_to_next(reason)
                    break

            # 10:00 前漲停偵測
            if ts.strftime("%H:%M") < ABANDON_LIMIT_UP_BY:
                if is_limit_up(price, self._ref_price):
                    print(f"  [SKIP] {ts.strftime('%H:%M')} 漲停，換下一檔")
                    self._switch_to_next("limit_up")
                    break

            # 進場判斷
            if not self._entered and not self._abandoned:
                result = self.engine.evaluate(
                    state=state,
                    strategy=self._active_stock["strategy"],
                    already_traded=False,
                )
                if result.signal:
                    ok = await self._executor.enter(self._active_stock, price, result.signal)
                    if ok:
                        self._entered = True
                        print(f"\n  ▶ 【進場】{ts.strftime('%H:%M:%S')}  "
                              f"{result.signal.upper()} @ {price:.2f}  "
                              f"信心:{result.confidence:.1f}")

            # 停損/停利檢查
            if self._entered:
                await self._check_exits(price)
                pos_list = [p for p in self.pm.open_positions if p.stock_id == sid]
                if not pos_list:
                    # 已平倉
                    print(f"  ◀ 【出場】{ts.strftime('%H:%M:%S')}  @ {price:.2f}")
                    self._entered = False
                    break

            await asyncio.sleep(0)  # yield to event loop

        # ── 13:25 強制平倉（若仍持倉）────────────────────────────────────
        if self._entered and self.pm.open_positions:
            last_price = self._state.last_price
            print(f"\n  ⏰ 13:25 強制平倉 @ {last_price:.2f}")
            await self._force_close()
            self._entered = False

        await self.stop()

    # ── 停止 ──────────────────────────────────────────────────────────────────
    async def stop(self) -> None:
        if not self.dry_run:
            await self.client.disconnect()
        await self.db.close()

        summary = self.pm.daily_summary()
        open_pos_pnl = sum(
            p.calc_pnl(self._state.last_price if self._state else p.entry_price)
            for p in self.pm.open_positions
        )

        print(f"\n{'='*55}")
        print(f"  📈 交易日報 — {self.custom_date or trading_date()}")
        print(f"{'='*55}")
        print(f"  主力標的    : [{self._active_stock['stock_id']}] {self._active_stock['stock_name']}"
              if self._active_stock else "  主力標的    : 無")
        print(f"  策略方向    : {self._active_stock['strategy'].upper()}"
              if self._active_stock else "")
        print(f"  進場次數    : {summary['total_trades']}")
        print(f"  已平倉      : {summary['closed']}")
        print(f"  已實現損益  : {summary.get('realized_pnl', 0.0):+.0f} 元")
        print(f"  未平倉損益  : {open_pos_pnl:+.0f} 元")
        print(f"{'='*55}\n")

        logger.info("daily_summary", **summary)


# ══════════════════════════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════════════════════════
async def main(args: argparse.Namespace) -> None:
    trader = DayTrader(dry_run=args.dry_run, custom_date=args.date)
    try:
        await trader.start()
    except KeyboardInterrupt:
        logger.info("keyboard_interrupt")
    except Exception as e:
        logger.error("trader_error", error=str(e), exc_info=True)
        raise


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="台股單檔當沖系統")
    p.add_argument("--dry-run", action="store_true",
                   help="模擬模式（不需要 Shioaji 憑證，注入 mock tick 跑完整策略）")
    p.add_argument("--date", type=str, default=None,
                   help="自訂交易日期（格式: YYYY-MM-DD，用於週末/過往日期模擬測試）")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if not args.dry_run:
        try:
            settings.require_shioaji()
        except EnvironmentError as e:
            print(f"\n[ERROR] {e}\n")
            sys.exit(1)

        print(f"\n⚠️  真實交易確認")
        print(f"   帳號：{settings.shioaji_account_id}")
        print(f"   環境：{settings.trading_env}")
        print(f"   停損：{settings.stop_loss_pct * 100:.1f}%  移動停利：1.5%")
        print(f"   主力標的：從 watchlist.json 讀取")
        if input("\n輸入 YES 繼續 > ").strip() != "YES":
            sys.exit(0)

    asyncio.run(main(args))
