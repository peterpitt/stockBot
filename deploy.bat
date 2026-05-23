@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

:: ==============================================================
::  Taiwan Stock Day-Trade System - Deploy Script (Windows)
::  台股當沖系統一鍵部署腳本
:: ==============================================================

echo.
echo  =============================================
echo   Taiwan Stock Day-Trade System - Deploy
echo   台股當沖系統 - 一鍵部署
echo  =============================================
echo.

:: ── Step 0: 檢查 Docker 是否可用 ────────────────────────────────
docker version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Docker 未啟動或未安裝
    echo.
    echo  請按以下步驟修復:
    echo  1. 確認已安裝 Docker Desktop
    echo     下載: https://www.docker.com/products/docker-desktop/
    echo.
    echo  2. 啟動 Docker Desktop 後，開啟設定:
    echo     Settings - Resources - WSL Integration
    echo     啟用 "Enable integration with my default WSL distro"
    echo.
    echo  3. 重新開啟此終端機後再執行
    pause
    exit /b 1
)
echo [OK] Docker 已就緒
docker version --format "    Engine: {{.Server.Version}}" 2>nul

:: ── Step 1: 檢查 .env 檔案 ──────────────────────────────────────
if not exist ".env" (
    echo.
    echo [WARN] 找不到 .env 檔案
    echo  正在從 .env.example 複製...
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo [OK] .env 已建立，請編輯填入您的 API 金鑰
        echo  路徑: %cd%\.env
        notepad .env
    ) else (
        echo [ERROR] .env.example 也不存在，請手動建立 .env
        pause
        exit /b 1
    )
)
echo [OK] .env 設定檔存在

:: ── Step 2: 建立必要目錄 ────────────────────────────────────────
if not exist "certs"           mkdir certs
if not exist "data\raw"        mkdir data\raw
if not exist "data\processed"  mkdir data\processed
if not exist "logs"            mkdir logs
echo [OK] 目錄結構已建立

:: ── Step 3: 檢查憑證 ────────────────────────────────────────────
if not exist "certs\Sinopac.pfx" (
    echo.
    echo [WARN] 找不到永豐金憑證 certs\Sinopac.pfx
    echo  請將 Sinopac.pfx 放入 certs\ 目錄後重新執行
    echo  （如使用模擬環境可忽略此警告）
    echo.
)

:: ── Step 4: 停止舊容器（如存在）────────────────────────────────
echo.
echo [INFO] 停止現有容器...
docker compose down >nul 2>&1

:: ── Step 5: 建構 Docker Image ───────────────────────────────────
echo [INFO] 建構 Docker Image（首次需要數分鐘）...
docker compose build --no-cache
if %errorlevel% neq 0 (
    echo [ERROR] Build 失敗，請檢查 Dockerfile
    pause
    exit /b 1
)
echo [OK] Image 建構完成

:: ── Step 6: 啟動服務 ────────────────────────────────────────────
echo [INFO] 啟動服務...
docker compose up -d
if %errorlevel% neq 0 (
    echo [ERROR] 服務啟動失敗
    docker compose logs --tail=30
    pause
    exit /b 1
)

:: ── Step 7: 確認狀態 ────────────────────────────────────────────
echo.
echo  =============================================
echo   部署成功！
echo  =============================================
docker compose ps
echo.
echo  常用指令:
echo    查看即時日誌: docker compose logs -f daytrade
echo    停止系統:     docker compose down
echo    手動選股:     docker compose exec daytrade python scripts/run_scanner.py
echo.
pause
