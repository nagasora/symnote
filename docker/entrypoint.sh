#!/bin/bash
# Why not: CMDよりENTRYPOINTの方がSIGTERM処理に安定するため採用

# .env を読み込む（grep が無い環境にも対応）
if [ -f "/app/.env" ]; then
  while IFS='=' read -r key value; do
    case "$key" in
      ''|\#*) continue;;
      *) export "$key"="$value";;
    esac
  done < "/app/.env"
fi

# Python パスを通す
export PYTHONPATH="/app/src:${PYTHONPATH}"

# Streamlit 起動
exec streamlit run src/symnote/app.py --server.address=0.0.0.0 --server.port=8501
