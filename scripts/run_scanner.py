"""
scripts/run_scanner.py
───────────────────────
盤後選股主程式。

執行方式（在任何目錄都可以）：
  python scripts/run_scanner.py --generate-sample
  python scripts/run_scanner.py
  cd scripts && python run_scanner.py --generate-sample   ← 也可以
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# ── 確保無論從哪個目錄執行，專案根目錄都在 sys.path ──────────────────────────
# __file__ = .../twse_daytrade/scripts/run_scanner.py
# .parent   = .../twse_daytrade/scripts/
# .parent.parent = .../twse_daytrade/   ← 專案根目錄
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

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

# ── 把 CWD 切到專案根目錄，確保相對路徑（如 data/）正確 ──────────────────────
os.chdir(PROJECT_ROOT)

from config.settings import get_settings
from src.scanner.branch_filter import BranchFilter
from src.scanner.watchlist_builder import WatchlistBuilder
from src.utils.logger import get_logger, setup_logging


async def main(args: argparse.Namespace) -> None:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_dir)
    logger = get_logger("run_scanner")

    logger.info(
        "scanner_start",
        project_root=str(PROJECT_ROOT),
        env_file=str(PROJECT_ROOT / ".env"),
        trading_env=settings.trading_env,
    )

    # ── 生成測試資料 ──────────────────────────────────────────────────────────
    if args.generate_sample:
        print(f"\n[INFO] 生成測試用 branch_data.csv ...")
        BranchFilter.generate_sample_csv(settings.branch_csv_path)

    # ── 確認 branch_data.csv 存在 ─────────────────────────────────────────────
    if not settings.branch_csv_path.exists():
        print(
            f"\n[WARN] 找不到分點資料：{settings.branch_csv_path}\n"
            f"       請執行：python scripts/run_scanner.py --generate-sample\n"
            f"       或提供真實的 branch_data.csv\n"
        )

    # ── 執行選股 ──────────────────────────────────────────────────────────────
    builder = WatchlistBuilder(
        branch_csv_path=settings.branch_csv_path,
        output_path=settings.watchlist_path,
    )
    watchlist = await builder.build(
        trust_consecutive_days=settings.trust_buy_consecutive_days,
        branch_buy_threshold=settings.branch_buy_threshold,
    )

    # ── 顯示結果摘要 ──────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"  📊 當沖觀察名單 — {watchlist['date']}")
    print("=" * 60)

    stats = watchlist.get("stats", {})
    print(f"  投信連買標的：{stats.get('trust_consecutive_count', 0)} 檔")
    print(f"  投信反轉標的：{stats.get('trust_reversal_count', 0)} 檔")
    print(f"  隔日沖分點：  {stats.get('branch_targets_count', 0)} 檔")
    print(f"  最終名單：    {stats.get('final_watchlist_count', 0)} 檔")
    print("-" * 60)

    for i, stock in enumerate(watchlist.get("stocks", []), 1):
        strategy_icon = "🟢 做多" if stock["strategy"] == "long" else "🔴 做空"
        sources = ", ".join(stock.get("signal_source", []))
        print(
            f"  {i:2d}. [{stock['stock_id']}] {stock['stock_name']:<8} "
            f"{strategy_icon}  P{stock['priority']}  {sources}"
        )
        if stock.get("trust_consecutive_days"):
            print(f"       投信連買 {stock['trust_consecutive_days']} 天")
        if stock.get("branch_confidence"):
            print(f"       分點信心 {stock['branch_confidence']:.1f}")

    print("=" * 60)
    print(f"  📁 Watchlist 已寫入：{settings.watchlist_path}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="台股盤後選股模組",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
範例：
  python scripts/run_scanner.py --generate-sample   # 生成測試資料並選股
  python scripts/run_scanner.py                     # 直接選股（需已有資料）
  cd scripts && python run_scanner.py --generate-sample
        """,
    )
    parser.add_argument(
        "--generate-sample",
        action="store_true",
        help="生成測試用 branch_data.csv（開發/測試用）",
    )
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
