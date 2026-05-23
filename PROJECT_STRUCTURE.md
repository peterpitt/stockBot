twse_daytrade/
│
├── .env                        # 機密憑證（絕不 commit）
├── .env.example                # 範本
├── .gitignore
├── requirements.txt
├── docker-compose.yml
├── Dockerfile
│
├── config/
│   ├── __init__.py
│   └── settings.py             # Pydantic BaseSettings 統一設定
│
├── src/
│   ├── __init__.py
│   │
│   ├── api/
│   │   ├── __init__.py
│   │   ├── shioaji_client.py   # Shioaji 連線、重連、訂閱管理
│   │   └── quote_dispatcher.py # Tick/K-line callback → asyncio.Queue
│   │
│   ├── scanner/
│   │   ├── __init__.py
│   │   ├── tpex_scraper.py     # 爬取上櫃投信買賣超
│   │   ├── branch_filter.py    # 分點券商過濾邏輯
│   │   └── watchlist_builder.py# 整合選股，輸出 watchlist.json
│   │
│   ├── strategy/
│   │   ├── __init__.py
│   │   ├── indicators.py       # VWAP, K-line 合成, MA
│   │   ├── signal_engine.py    # 進場/出場訊號判斷
│   │   └── position_manager.py # 部位追蹤、停損/停利
│   │
│   ├── db/
│   │   ├── __init__.py
│   │   ├── database.py         # SQLite / Supabase 抽象層
│   │   └── models.py           # ORM 資料模型
│   │
│   └── utils/
│       ├── __init__.py
│       ├── logger.py           # structlog 結構化日誌
│       ├── retry.py            # tenacity 重試裝飾器
│       └── time_utils.py       # 台股交易時段判斷
│
├── data/
│   ├── raw/                    # 原始爬蟲資料（CSV）
│   │   └── branch_data.csv
│   └── processed/              # 處理後資料 & watchlist
│       └── watchlist.json
│
├── logs/                       # 結構化 JSON 日誌
│
├── tests/
│   ├── test_scanner.py
│   ├── test_indicators.py
│   └── test_strategy.py
│
├── scripts/
│   ├── run_scanner.py          # 每日盤後執行選股
│   └── run_trader.py           # 盤中執行交易
│
└── trader.py                   # 主程式入口（第三步）
