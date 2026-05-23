"""
main.py
────────
系統排程主程式。
- 每日 20:00 執行盤後選股
- 每日 08:30 準備交易環境
- 每日 09:00 啟動當沖交易（第三步實作）
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

sys.path.insert(0, str(Path(__file__).parent))

# ── 確保 stdout 與 stderr 使用 UTF-8 編碼 ──
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
from src.scanner.branch_filter import BranchFilter
from src.scanner.watchlist_builder import run_scanner
from src.utils.logger import get_logger, setup_logging
from src.utils.time_utils import TW_TZ

settings = get_settings()
setup_logging(settings.log_level, settings.log_dir)
logger = get_logger("main")


async def job_scanner() -> None:
    """盤後選股任務（每日 20:00 執行）"""
    logger.info("scheduled_scanner_start")
    try:
        # 確保測試資料存在（production 中替換為真實資料來源）
        branch_csv = Path("data/raw/branch_data.csv")
        if not branch_csv.exists():
            logger.warning("branch_csv_missing", msg="使用空資料集，請提供真實分點資料")
            BranchFilter.generate_sample_csv(branch_csv)

        watchlist = await run_scanner(
            trust_consecutive_days=settings.trust_buy_consecutive_days,
            branch_buy_threshold=settings.branch_buy_threshold,
        )
        logger.info(
            "scheduled_scanner_done",
            stock_count=len(watchlist.get("stocks", [])),
        )
    except Exception as e:
        logger.error("scheduled_scanner_error", error=str(e), exc_info=True)


async def job_pre_market() -> None:
    """開盤前準備（08:30）"""
    logger.info("pre_market_prep_start")
    # 第三步：trader.py 會在此載入 watchlist 並訂閱行情


async def main() -> None:
    logger.info(
        "system_start",
        env=settings.trading_env,
        version="1.0.0",
    )

    scheduler = AsyncIOScheduler(timezone=TW_TZ)

    # 每日 20:00 — 盤後選股
    scheduler.add_job(
        job_scanner,
        CronTrigger(hour=20, minute=0, timezone=TW_TZ),
        id="scanner",
        name="盤後選股",
        misfire_grace_time=300,
    )

    # 每日 08:30 — 開盤前準備
    scheduler.add_job(
        job_pre_market,
        CronTrigger(hour=8, minute=30, timezone=TW_TZ),
        id="pre_market",
        name="開盤前準備",
        misfire_grace_time=120,
    )

    scheduler.start()
    logger.info("scheduler_started", jobs=["盤後選股@20:00", "開盤前準備@08:30"])

    # 啟動時立即執行一次選股（方便測試）
    if "--run-now" in sys.argv:
        logger.info("immediate_scanner_triggered")
        await job_scanner()

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        logger.info("system_shutdown")
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
