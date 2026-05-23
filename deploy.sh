#!/usr/bin/env bash
# deploy.sh — 台股當沖系統部署腳本（WSL / Linux / macOS）
# 解決問題：WSL 中 Docker Desktop 未整合時的降級處理
set -euo pipefail

# ── 顏色 ──────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'

log_ok()   { echo -e "${GREEN}[OK]${RESET}  $*"; }
log_err()  { echo -e "${RED}[ERR]${RESET} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${RESET} $*"; }
log_info() { echo -e "${BLUE}[INFO]${RESET} $*"; }

echo -e "${BOLD}"
echo "  ============================================="
echo "   台股當沖系統 — 一鍵部署 (WSL/Linux/macOS)"
echo "  ============================================="
echo -e "${RESET}"

# ── Step 0: 檢查 Docker ───────────────────────────────────────────────────────
DOCKER_OK=false

if command -v docker &>/dev/null; then
    if docker info &>/dev/null 2>&1; then
        DOCKER_OK=true
        log_ok "Docker 已就緒 ($(docker version --format '{{.Server.Version}}' 2>/dev/null || echo 'unknown'))"
    else
        log_warn "docker 指令存在，但 daemon 無法連線"
    fi
fi

if [ "$DOCKER_OK" = false ]; then
    log_warn "Docker 無法使用，改用本機 Python 模式"
    echo
    echo "  WSL 用戶修復方法："
    echo "  1. 開啟 Docker Desktop（Windows）"
    echo "  2. Settings → Resources → WSL Integration"
    echo "     勾選你的 WSL distro（如 Ubuntu）"
    echo "  3. 重新開啟 WSL 終端機"
    echo
    echo "  或直接以本機 Python 啟動（不需 Docker）："
    echo "  ─────────────────────────────────────────"
    USE_LOCAL=true
else
    USE_LOCAL=false
fi

# ── Step 1: 目錄結構 ──────────────────────────────────────────────────────────
mkdir -p certs data/raw data/processed logs
log_ok "目錄結構已建立"

# ── Step 2: .env 檔案 ─────────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        log_warn ".env 已從範本建立，請填入真實的 API 金鑰"
        echo "  vim .env 或 nano .env"
    else
        log_err ".env 與 .env.example 均不存在"
        exit 1
    fi
else
    log_ok ".env 設定檔存在"
fi

# ── Step 3: Python 環境（本機模式或虛擬環境）─────────────────────────────────
setup_python_env() {
    echo
    log_info "設定 Python 環境..."

    PYTHON_CMD=""
    for py in python3.11 python3.10 python3 python; do
        if command -v "$py" &>/dev/null; then
            ver=$($py -c "import sys; print(sys.version_info >= (3,10))" 2>/dev/null)
            if [ "$ver" = "True" ]; then
                PYTHON_CMD="$py"
                log_ok "使用 $py ($(${py} --version))"
                break
            fi
        fi
    done

    if [ -z "$PYTHON_CMD" ]; then
        log_err "需要 Python 3.10+，請先安裝"
        exit 1
    fi

    # 建立虛擬環境
    if [ ! -d ".venv" ]; then
        log_info "建立虛擬環境 .venv ..."
        $PYTHON_CMD -m venv .venv
    fi

    # 啟動並安裝依賴
    source .venv/bin/activate
    pip install --upgrade pip -q
    pip install -r requirements.txt -q
    log_ok "Python 依賴安裝完成"
}

# ── Docker 模式 ───────────────────────────────────────────────────────────────
run_docker() {
    log_info "停止現有容器..."
    docker compose down 2>/dev/null || true

    log_info "建構 Docker Image..."
    docker compose build --no-cache

    log_info "啟動服務..."
    docker compose up -d

    echo
    echo -e "${GREEN}${BOLD}  ✅ 部署成功！${RESET}"
    echo
    docker compose ps
    echo
    echo "  常用指令："
    echo "    即時日誌：docker compose logs -f daytrade"
    echo "    停止服務：docker compose down"
    echo "    手動選股：docker compose exec daytrade python scripts/run_scanner.py --generate-sample"
}

# ── 本機 Python 模式 ──────────────────────────────────────────────────────────
run_local() {
    setup_python_env
    source .venv/bin/activate

    echo
    echo -e "${GREEN}${BOLD}  ✅ 環境就緒！${RESET}"
    echo
    echo "  啟動指令："
    echo "    source .venv/bin/activate"
    echo
    echo "  盤後選股（先生成測試資料）："
    echo "    python scripts/run_scanner.py --generate-sample"
    echo
    echo "  每日排程："
    echo "    python main.py"
    echo
    read -rp "  現在執行測試選股？[y/N] " confirm
    if [[ "${confirm:-n}" =~ ^[Yy]$ ]]; then
        python scripts/run_scanner.py --generate-sample
    fi
}

# ── 執行 ──────────────────────────────────────────────────────────────────────
if [ "$USE_LOCAL" = true ]; then
    run_local
else
    run_docker
fi
