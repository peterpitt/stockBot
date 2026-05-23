# 📈 台股單檔當沖自動化交易系統

這是一套基於 **永豐金證券 Shioaji API** 開發的台股單檔當沖交易系統。本系統採用 **Docker 容器化** 與 **GitHub Actions CI/CD**，保證在不同作業系統（Windows / Mac / Linux / WSL）下，皆能實現「**開箱即用、一鍵部署**」的跨平台穩定執行。

---

## 🚀 快速上手部署指南 (其他電腦 Clone 後直接使用)

當您或您的協作者將本專案 `git clone` 到全新電腦後，只需以下三步即可直接運行：

### 步驟 1：放置您的憑證金鑰 🔑
請在專案根目錄下，將您的永豐金 API 憑證檔案放入 `certs/` 資料夾中。
- 預期路徑：`certs/Sinopac.pfx`

### 步驟 2：配置環境變數 ⚙️
複製設定範本檔案並命名為 `.env`，接著以文字編輯器打開它，填入您的真實帳密：
```bash
# Windows 命令提示字元 (CMD)
copy .env.example .env

# Mac / Linux / Git Bash / PowerShell
cp .env.example .env
```
填寫 `.env` 中的核心變數：
- `SHIOAJI_API_KEY`: 您的永豐金 API 金鑰
- `SHIOAJI_SECRET_KEY`: 您的永豐金 API 密鑰
- `SHIOAJI_ACCOUNT_ID`: 您的身分證字號 (例如 `E123456789`)
- `SHIOAJI_CA_PASSWD`: 您的憑證密碼
- `SHIOAJI_CA_PATH`: `./certs/Sinopac.pfx`

### 步驟 3：一鍵啟動部署 🐳
系統已為您打包好全自動腳本，會自動編譯 Docker 容器並掛載憑證與資料庫：
- **Windows 電腦**：直接雙擊執行 `deploy.bat`
- **Mac / Linux / WSL 電腦**：在終端機執行：
  ```bash
  chmod +x deploy.sh
  ./deploy.sh
  ```

---

## 🧪 週末模擬與策略測試 (Dry Run)

本系統支援強大的**離線模擬模式 (Dry Run)**，即便在週末或非開盤時段，亦可測試完整策略。

### 1. 產生觀察名單 (Scanner)
系統會自動抓取最新交易日（如 5/22 週五）的籌碼數據來產生 watchlist：
```bash
docker compose run --rm daytrade python scripts/run_scanner.py --generate-sample
```

### 2. 啟動當沖策略模擬 (Trader)
利用 `--date` 參數，即可將模擬交易日報表的日期精準設定為該交易日（如 5/22）：
```bash
docker compose run --rm daytrade python trader.py --dry-run --date 2026-05-22
```

---

## 🛠️ 開發人員實用指令

### 執行單元測試 (Pytest)
本專案有完善的單元測試，測試時區、開收盤時段、Pydantic 配置與實現損益的淨額計算（含手續費與稅金）：
```bash
# 在本機執行測試
pytest

# 在 Docker 容器內執行測試
docker compose run --rm daytrade pytest
```

### 重建 Docker 映像檔 (當您修改了程式碼時)
```bash
docker compose build
```

---

## 🔒 安全性宣告
為了保護您的財產與帳號安全，專案內置的 `.gitignore` 會**自動且嚴格地屏蔽**您的 `.env`、`certs/*.pfx` 以及本地生成的所有 SQLite 交易資料庫（`*.db`）與日誌檔案，請放心上傳至您的私有或公有 GitHub 倉庫！
