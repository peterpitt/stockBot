"""
src/scanner/branch_filter.py
──────────────────────────────
分點券商過濾邏輯。

輸入：branch_data.csv（第三方資料或自行爬蟲）
預期 CSV 格式：
    date,stock_id,stock_name,branch_name,buy_volume,sell_volume,net_volume
    2024-05-20,2330,台積電,凱基台北,1200,50,1150
    ...

核心功能：
1. 辨識「已知隔日沖分點」的大量買超
2. 計算各分點集中度（避免假訊號）
3. 輸出買超標的清單 + 信心分數
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from src.utils.logger import get_logger

logger = get_logger(__name__)


# ── 已知隔日沖分點特徵 ────────────────────────────────────────────────────────
# 這些分點以「買超後次日往往大量賣出」聞名，是隔日沖主力常用據點
# 實務上需定期維護此名單（可從資料庫動態載入）
DEFAULT_DAYTRADE_BRANCHES = [
    "凱基台北",
    "凱基士林",
    "元富",
    "元富台中",
    "群益金鼎",
    "群益台中",
    "永豐金",
    "永豐金中壢",
    "富邦",
    "富邦中山",
    "國泰",
    "國泰台北",
    "兆豐",
    "統一",
    "台新",
]


class BranchFilter:
    """
    分點券商過濾器。
    
    Usage:
        bf = BranchFilter(csv_path="data/raw/branch_data.csv")
        result = bf.filter_daytrade_targets(
            target_date="2024-05-20",
            buy_threshold=500,
        )
    """

    def __init__(
        self,
        csv_path: str | Path = "data/raw/branch_data.csv",
        daytrade_branches: Optional[list[str]] = None,
    ) -> None:
        self.csv_path = Path(csv_path)
        self.daytrade_branches = daytrade_branches or DEFAULT_DAYTRADE_BRANCHES
        self._raw: Optional[pd.DataFrame] = None

    # ── 載入 CSV ─────────────────────────────────────────────────────────────
    def load(self) -> "BranchFilter":
        """載入並預處理 branch_data.csv。"""
        if not self.csv_path.exists():
            logger.warning(
                "branch_csv_not_found",
                path=str(self.csv_path),
            )
            self._raw = pd.DataFrame()
            return self

        df = pd.read_csv(
            self.csv_path,
            dtype={
                "stock_id": str,
                "stock_name": str,
                "branch_name": str,
            },
            parse_dates=["date"],
        )

        # ── 欄位驗證 ──────────────────────────────────────────────────────────
        required_cols = {
            "date", "stock_id", "stock_name",
            "branch_name", "buy_volume", "sell_volume", "net_volume",
        }
        missing = required_cols - set(df.columns)
        if missing:
            raise ValueError(
                f"branch_data.csv 缺少欄位: {missing}\n"
                f"現有欄位: {list(df.columns)}"
            )

        # ── 清理 ──────────────────────────────────────────────────────────────
        df["stock_id"] = df["stock_id"].str.strip()
        df["branch_name"] = df["branch_name"].str.strip()
        for col in ["buy_volume", "sell_volume", "net_volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)

        self._raw = df
        logger.info(
            "branch_csv_loaded",
            path=str(self.csv_path),
            rows=len(df),
            date_range=f"{df['date'].min().date()} ~ {df['date'].max().date()}",
        )
        return self

    def _ensure_loaded(self) -> pd.DataFrame:
        if self._raw is None:
            self.load()
        return self._raw  # type: ignore[return-value]

    # ── 核心過濾：隔日沖分點買超 ─────────────────────────────────────────────
    def filter_daytrade_targets(
        self,
        target_date: Optional[str] = None,
        buy_threshold: int = 500,
        min_branches: int = 1,
        concentration_floor: float = 0.3,
    ) -> pd.DataFrame:
        """
        過濾被隔日沖分點大量買超的標的。

        Args:
            target_date:        目標日期 'YYYY-MM-DD'，None 表示最新日期
            buy_threshold:      單分點買超張數門檻
            min_branches:       至少幾個已知分點同時買超
            concentration_floor:隔日沖分點買超佔該股當日總買超比例下限

        Returns:
            DataFrame columns:
                stock_id, stock_name, daytrade_net_volume,
                daytrade_branch_count, concentration_ratio,
                branches_list, confidence_score
        """
        df = self._ensure_loaded()
        if df.empty:
            return pd.DataFrame()

        # ── 日期過濾 ──────────────────────────────────────────────────────────
        if target_date:
            mask = df["date"].dt.strftime("%Y-%m-%d") == target_date
        else:
            latest = df["date"].max()
            mask = df["date"] == latest
            target_date = str(latest.date())

        day_df = df[mask].copy()
        if day_df.empty:
            logger.warning("branch_no_data_for_date", date=target_date)
            return pd.DataFrame()

        # ── Step 1: 過濾已知隔日沖分點 ───────────────────────────────────────
        dt_mask = day_df["branch_name"].isin(self.daytrade_branches)
        dt_df = day_df[dt_mask & (day_df["net_volume"] >= buy_threshold)].copy()

        if dt_df.empty:
            logger.info("branch_no_daytrade_targets", date=target_date)
            return pd.DataFrame()

        # ── Step 2: 聚合到股票層級 ────────────────────────────────────────────
        agg = (
            dt_df.groupby(["stock_id", "stock_name"])
            .agg(
                daytrade_net_volume=("net_volume", "sum"),
                daytrade_branch_count=("branch_name", "nunique"),
                branches_list=("branch_name", lambda x: list(x.unique())),
            )
            .reset_index()
        )

        # ── Step 3: 計算集中度（隔日沖分點買超 / 該股全分點總買超）───────────
        total_buy_by_stock = (
            day_df[day_df["net_volume"] > 0]
            .groupby("stock_id")["net_volume"]
            .sum()
            .rename("total_net_volume")
        )
        agg = agg.merge(total_buy_by_stock, on="stock_id", how="left")
        agg["concentration_ratio"] = (
            agg["daytrade_net_volume"] / agg["total_net_volume"].clip(lower=1)
        ).round(4)

        # ── Step 4: 過濾條件 ──────────────────────────────────────────────────
        agg = agg[
            (agg["daytrade_branch_count"] >= min_branches)
            & (agg["concentration_ratio"] >= concentration_floor)
        ]

        # ── Step 5: 計算信心分數（0-100）─────────────────────────────────────
        # 信心分數 = 分點數量權重(40%) + 集中度(40%) + 買超張數相對值(20%)
        max_vol = agg["daytrade_net_volume"].max() or 1
        agg["confidence_score"] = (
            (agg["daytrade_branch_count"].clip(upper=5) / 5) * 40
            + agg["concentration_ratio"].clip(upper=1.0) * 40
            + (agg["daytrade_net_volume"] / max_vol) * 20
        ).round(1)

        agg["date"] = target_date
        agg = agg.sort_values("confidence_score", ascending=False).reset_index(drop=True)

        logger.info(
            "branch_filter_done",
            date=target_date,
            targets_found=len(agg),
            top3=agg["stock_id"].head(3).tolist(),
        )
        return agg[
            [
                "stock_id", "stock_name", "date",
                "daytrade_net_volume", "daytrade_branch_count",
                "concentration_ratio", "branches_list", "confidence_score",
            ]
        ]

    # ── 輔助：查詢特定股票的歷史分點紀錄 ─────────────────────────────────────
    def get_stock_branch_history(
        self,
        stock_id: str,
        days: int = 5,
    ) -> pd.DataFrame:
        """查詢特定股票最近 N 天的分點進出明細。"""
        df = self._ensure_loaded()
        if df.empty:
            return pd.DataFrame()

        filtered = df[df["stock_id"] == stock_id].copy()
        if filtered.empty:
            return pd.DataFrame()

        latest_dates = filtered["date"].drop_duplicates().nlargest(days)
        result = (
            filtered[filtered["date"].isin(latest_dates)]
            .sort_values(["date", "net_volume"], ascending=[False, False])
        )
        return result

    # ── 輔助：生成示範 CSV（供測試用）─────────────────────────────────────────
    @staticmethod
    def generate_sample_csv(output_path: str | Path = "data/raw/branch_data.csv") -> None:
        """生成測試用的 branch_data.csv 範本。"""
        import random
        from datetime import date, timedelta

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        branches = DEFAULT_DAYTRADE_BRANCHES + [
            "元大台北", "元大新竹", "中信台北", "日盛台北",
            "玉山台北", "台灣工銀", "陽信", "第一金"
        ]
        stocks = [
            ("6547", "中菲行"), ("6523", "達運"), ("4526", "東台"),
            ("6271", "同欣電"), ("3231", "緯創"), ("5225", "東生華"),
            ("4968", "立積"), ("6510", "精測"), ("3264", "欣銓"),
            ("6770", "力積電"),
        ]

        rows = []
        today = date.today()
        for i in range(5):
            d = today - timedelta(days=i + 1)
            if d.weekday() >= 5:
                continue
            for stock_id, stock_name in stocks:
                for branch in random.sample(branches, k=random.randint(3, 8)):
                    buy = random.randint(0, 2000)
                    sell = random.randint(0, 1000)
                    rows.append({
                        "date": d.strftime("%Y-%m-%d"),
                        "stock_id": stock_id,
                        "stock_name": stock_name,
                        "branch_name": branch,
                        "buy_volume": buy,
                        "sell_volume": sell,
                        "net_volume": buy - sell,
                    })

        pd.DataFrame(rows).to_csv(output_path, index=False, encoding="utf-8-sig")
        print(f"✅ 範本 CSV 已生成：{output_path}（{len(rows)} 筆）")
