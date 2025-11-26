FROM python:3.12-slim

# Why not: alpineだと依存関係が面倒なので slim ベースで安定性優先。
WORKDIR /app

# 必要ライブラリのシステム依存を先に導入（sqlite3など）
RUN apt-get update && \
    apt-get install -y build-essential curl sqlite3 && \
    rm -rf /var/lib/apt/lists/*

# Python ライブラリインストール
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# アプリをコンテナにコピー
COPY . /app/

# Entrypoint（後述）
RUN chmod +x docker/entrypoint.sh

EXPOSE 8501

ENTRYPOINT ["docker/entrypoint.sh"]
