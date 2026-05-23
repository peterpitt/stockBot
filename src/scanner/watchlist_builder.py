"""
src/scanner/watchlist_builder.py
─────────────────────────────────
策略修訂版：
- 每日只選一檔（優先順序：P1 > P2 > P3，同優先級取信心分最高）
- 價位篩選：10~70 元
- 輸出 watchlist.json（單檔）
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from src.scanner.branch_filter import BranchFilter
from src.scanner.tpex_scraper import TPExScraper
from src.utils.logger import get_logger
from src.utils.time_utils import TW_TZ

logger = get_logger(__name__)

WATCHLIST_PATH = Path("data/processed/watchlist.json")

PRICE_MIN = 10.0
PRICE_MAX = 70.0


class WatchlistBuilder:
    def __init__(
        self,
        branch_csv_path: str | Path = "data/raw/branch_data.csv",
        output_path: str | Path = WATCHLIST_PATH,
        daytrade_branches: Optional[list[str]] = None,
    ) -> None:
        self.branch_csv_path = Path(branch_csv_path)
        self.output_path = Path(output_path)
        self.branch_filter = BranchFilter(
            csv_path=branch_csv_path,
            daytrade_branches=daytrade_branches,
        )

    @staticmethod
    def calc_consecutive_buy(
        history_df: pd.DataFrame,
        min_consecutive: int = 3,
        min_net_volume: int = 50,
    ) -> pd.DataFrame:
        if history_df.empty:
            return pd.DataFrame()

        df = history_df.copy().sort_values(["stock_id", "date"])
        df["is_buy"] = df["trust_net"] >= min_net_volume
        results = []

        for stock_id, group in df.groupby("stock_id"):
            group = group.reset_index(drop=True)
            consecutive = 0
            for _, row in group[::-1].iterrows():
                if row["is_buy"]:
                    consecutive += 1
                else:
                    break
            if consecutive >= min_consecutive:
                latest = group.iloc[-1]
                recent = group[group["is_buy"]].tail(consecutive)
                results.append({
                    "stock_id": stock_id,
                    "stock_name": latest["stock_name"],
                    "consecutive_days": consecutive,
                    "total_net_volume": int(recent["trust_net"].sum()),
                    "latest_net": int(latest["trust_net"]),
                    "latest_date": str(latest["date"].date()
                                      if hasattr(latest["date"], "date")
                                      else latest["date"]),
                })

        result_df = pd.DataFrame(results)
        if not result_df.empty:
            result_df = result_df.sort_values("consecutive_days", ascending=False).reset_index(drop=True)
        logger.info("trust_consecutive_buy_found", count=len(result_df))
        return result_df

    @staticmethod
    def calc_trust_reversal(history_df: pd.DataFrame, lookback_days: int = 3) -> pd.DataFrame:
        if history_df.empty:
            return pd.DataFrame()

        df = history_df.copy().sort_values(["stock_id", "date"])
        reversals = []

        for stock_id, group in df.groupby("stock_id"):
            group = group.reset_index(drop=True)
            if len(group) < lookback_days:
                continue
            recent = group.tail(lookback_days)
            prev_days = recent.iloc[:-1]
            last_day = recent.iloc[-1]
            if (prev_days["trust_net"] > 0).all() and last_day["trust_net"] < -50:
                reversals.append({
                    "stock_id": stock_id,
                    "stock_name": last_day["stock_name"],
                    "reversal_date": str(last_day["date"].date()
                                        if hasattr(last_day["date"], "date")
                                        else last_day["date"]),
                    "last_net": int(last_day["trust_net"]),
                    "prior_avg_net": float(prev_days["trust_net"].mean()),
                })

        result_df = pd.DataFrame(reversals)
        logger.info("trust_reversal_found", count=len(result_df))
        return result_df

    async def build(
        self,
        trust_consecutive_days: int = 3,
        branch_buy_threshold: int = 500,
    ) -> dict:
        today = date.today()
        scan_date = today - timedelta(days=1)
        logger.info("watchlist_build_start", scan_date=str(scan_date))

        async with TPExScraper() as scraper:
            trust_history_task = scraper.fetch_history(days=max(trust_consecutive_days + 2, 7))
            branch_task = asyncio.to_thread(
                self._run_branch_filter, str(scan_date), branch_buy_threshold
            )
            trust_history, branch_result = await asyncio.gather(
                trust_history_task, branch_task, return_exceptions=True
            )

        if isinstance(trust_history, Exception):
            logger.error("trust_fetch_failed", error=str(trust_history))
            trust_history = pd.DataFrame()
        if isinstance(branch_result, Exception):
            logger.error("branch_filter_failed", error=str(branch_result))
            branch_result = pd.DataFrame()

        trust_consecutive = self.calc_consecutive_buy(trust_history, min_consecutive=trust_consecutive_days)
        trust_reversal = self.calc_trust_reversal(trust_history)

        all_candidates = self._merge_signals(trust_consecutive, trust_reversal, branch_result)

        # ── 價位篩選 10~70 元（需要收盤價，branch_data.csv 若有收盤價欄位則用）
        # 目前 branch_data 沒有收盤價，先保留全部候選，實盤時可串接報價 API 篩選
        # 此處在 watchlist 裡標記 price_filter_required = true，由 trader.py 開盤前確認
        for s in all_candidates:
            s["price_range"] = f"{PRICE_MIN}~{PRICE_MAX}"
            s["price_filter_required"] = True

        # ── 每日只選一檔：P1 > P2 > P3，同級取信心分最高 ──────────────────
        selected = all_candidates[0] if all_candidates else None

        watchlist = {
            "date": str(today),
            "scan_date": str(scan_date),
            "generated_at": datetime.now(tz=TW_TZ).isoformat(),
            "mode": "single_stock_per_day",
            "price_range": f"{PRICE_MIN}~{PRICE_MAX}",
            "stats": {
                "trust_consecutive_count": len(trust_consecutive),
                "trust_reversal_count": len(trust_reversal),
                "branch_targets_count": len(branch_result),
                "all_candidates_count": len(all_candidates),
            },
            "selected": selected,          # 今日主力標的
            "backup": all_candidates[1:5], # 備用（漲停時依序替換）
            "stocks": all_candidates,      # 保留完整清單相容舊程式
        }

        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.output_path, "w", encoding="utf-8") as f:
            json.dump(watchlist, f, ensure_ascii=False, indent=2)

        logger.info(
            "watchlist_saved",
            selected=selected["stock_id"] if selected else None,
            backup_count=len(watchlist["backup"]),
        )
        return watchlist

    def _run_branch_filter(self, target_date: str, buy_threshold: int) -> pd.DataFrame:
        self.branch_filter.load()
        return self.branch_filter.filter_daytrade_targets(
            target_date=target_date,
            buy_threshold=buy_threshold,
        )

    def _merge_signals(
        self,
        trust_consecutive: pd.DataFrame,
        trust_reversal: pd.DataFrame,
        branch_result: pd.DataFrame,
    ) -> list[dict]:
        stocks: dict[str, dict] = {}

        if not trust_consecutive.empty:
            for _, row in trust_consecutive.iterrows():
                sid = row["stock_id"]
                stocks[sid] = {
                    "stock_id": sid,
                    "stock_name": row["stock_name"],
                    "strategy": "long",
                    "signal_source": ["trust_consecutive_buy"],
                    "trust_consecutive_days": int(row["consecutive_days"]),
                    "trust_net_volume": int(row["total_net_volume"]),
                    "branch_confidence": 0.0,
                    "priority": 2,
                }

        if not branch_result.empty:
            for _, row in branch_result.iterrows():
                sid = row["stock_id"]
                conf = float(row["confidence_score"])
                priority = 1 if conf >= 70 else 3
                if sid in stocks:
                    stocks[sid]["strategy"] = "short"
                    stocks[sid]["signal_source"].append("daytrade_branch")
                    stocks[sid]["branch_confidence"] = conf
                    stocks[sid]["branches_list"] = row["branches_list"]
                    stocks[sid]["priority"] = 1
                else:
                    stocks[sid] = {
                        "stock_id": sid,
                        "stock_name": row["stock_name"],
                        "strategy": "short",
                        "signal_source": ["daytrade_branch"],
                        "trust_consecutive_days": 0,
                        "trust_net_volume": 0,
                        "branch_confidence": conf,
                        "branches_list": row["branches_list"],
                        "priority": priority,
                    }

        if not trust_reversal.empty:
            for _, row in trust_reversal.iterrows():
                sid = row["stock_id"]
                if sid not in stocks:
                    stocks[sid] = {
                        "stock_id": sid,
                        "stock_name": row["stock_name"],
                        "strategy": "short",
                        "signal_source": ["trust_reversal"],
                        "trust_consecutive_days": 0,
                        "trust_net_volume": int(row["last_net"]),
                        "branch_confidence": 0.0,
                        "priority": 2,
                    }
                else:
                    stocks[sid]["signal_source"].append("trust_reversal")

        return sorted(
            stocks.values(),
            key=lambda x: (x["priority"], -x["branch_confidence"], -x["trust_consecutive_days"]),
        )


async def run_scanner(
    branch_csv_path: str = "data/raw/branch_data.csv",
    output_path: str = "data/processed/watchlist.json",
    trust_consecutive_days: int = 3,
    branch_buy_threshold: int = 500,
) -> dict:
    builder = WatchlistBuilder(branch_csv_path=branch_csv_path, output_path=output_path)
    return await builder.build(
        trust_consecutive_days=trust_consecutive_days,
        branch_buy_threshold=branch_buy_threshold,
    )
