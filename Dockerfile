# syntax=docker/dockerfile:1
# ─────────────────────────────────────────────
# 台股當沖系統 Dockerfile
# Base: Python 3.11 slim (穩定、體積小)
# ─────────────────────────────────────────────
FROM python:3.11-slim

# 時區設定：台北
ENV TZ=Asia/Taipei \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PYTHONPATH=/app

WORKDIR /app

# 安裝系統依賴（lxml 需要 libxml2）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2-dev \
    libxslt-dev \
    curl \
    tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 安裝 Python 依賴（分層 cache）
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# 複製原始碼
COPY . .

# 建立必要目錄
RUN mkdir -p data/raw data/processed logs certs

# 預設啟動：排程器（包含盤後選股 + 盤中交易）
CMD ["python", "main.py"]
