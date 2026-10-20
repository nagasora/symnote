#!/usr/bin/env bash
# SymNote 招待制ベータ（Cloudflare Named Tunnel + Web サーバー）セットアップ補助スクリプト
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

echo "=== SymNote 招待制ベータ セットアップチェック ==="

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "[!] cloudflared が見つかりません。以下でインストールしてください:"
  echo "    brew install cloudflared"
  exit 1
fi
echo "[OK] cloudflared: $(command -v cloudflared)"

if [ ! -d "$ROOT_DIR/web/dist" ]; then
  echo "[*] フロントエンド (web/dist) をビルドしています..."
  (cd "$ROOT_DIR/web" && npm install && npm run build)
fi
echo "[OK] web/dist ビルド済み"

echo ""
echo "次のステップ（Cloudflare トンネル作成・起動）:"
echo "  1. cloudflared tunnel login"
echo "  2. cloudflared tunnel create symnote"
echo "  3. cloudflared tunnel route dns symnote app.symnote.app"
echo "  4. .venv/bin/python -m symnote.admin_cli invite <あなたのGmail> --owner"
echo "  5. .venv/bin/python -m symnote.web_server --host 127.0.0.1 --port 8000"
echo "詳しくは docs/RELEASE.md を参照してください。"
