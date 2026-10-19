#!/bin/zsh
# SymNote 一式を起動する：画面（Streamlit）、ChatGPT/Claude 用のリモート MCP、Cloudflare トンネル。
# すでに動いているものは起動し直さない。ログは ~/Library/Logs/SymNote/ に残る。
set -u
root="${0:A:h:h}"
py="$root/.venv/bin/python"
env_file="$root/src/symnote/.env"
logs="$HOME/Library/Logs/SymNote"
app_port=8510
mcp_port="$(grep -E '^SYMNOTE_MCP_PORT=' "$env_file" 2>/dev/null | cut -d= -f2)"
mcp_port="${mcp_port:-8765}"
mkdir -p "$logs"
export PYTHONPATH="$root/src"

listening() { lsof -iTCP:"$1" -sTCP:LISTEN -P >/dev/null 2>&1; }

if ! listening "$app_port"; then
  (cd "$root" && nohup "$py" -m streamlit run src/symnote/app.py --server.address 127.0.0.1 \
    --server.port "$app_port" --server.headless true --browser.gatherUsageStats false \
    >"$logs/streamlit.log" 2>&1 &)
fi

tunnel_url=""
if pgrep -f "cloudflared tunnel --no-autoupdate --url http://127.0.0.1:$mcp_port" >/dev/null; then
  tunnel_url="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$logs/cloudflared-quick.log" | tail -1)"
elif command -v cloudflared >/dev/null; then
  nohup cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$mcp_port" >"$logs/cloudflared-quick.log" 2>&1 &
  for _ in {1..30}; do
    tunnel_url="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$logs/cloudflared-quick.log" | head -1)"
    [[ -n "$tunnel_url" ]] && break
    sleep 1
  done
fi

current_url="$(grep -E '^SYMNOTE_MCP_PUBLIC_URL=' "$env_file" | cut -d= -f2-)"
if [[ -n "$tunnel_url" && "$tunnel_url" != "$current_url" && "$current_url" == *trycloudflare.com* ]]; then
  # 一時 URL はトンネルを起動するたびに変わるため、この 1 行だけ書き換える（独自ドメイン設定は触らない）。
  mkdir -p "$root/backups"
  backup="$root/backups/env.before-url-change.$(date +%Y%m%d-%H%M%S).bak"
  tmp_env="$env_file.tmp.$$"
  if cp -p "$env_file" "$backup" \
    && sed "s#^SYMNOTE_MCP_PUBLIC_URL=.*#SYMNOTE_MCP_PUBLIC_URL=$tunnel_url#" "$env_file" >"$tmp_env" \
    && grep -qx "SYMNOTE_MCP_PUBLIC_URL=$tunnel_url" "$tmp_env" \
    && [[ "$(wc -l <"$tmp_env")" -eq "$(wc -l <"$env_file")" ]] \
    && chmod "$(stat -f %Lp "$env_file")" "$tmp_env" && mv "$tmp_env" "$env_file"; then
    pkill -f "symnote.mcp_remote" 2>/dev/null
    sleep 1
  else
    rm -f "$tmp_env"
    print "⚠️ .env を更新できませんでした。SYMNOTE_MCP_PUBLIC_URL を手動で $tunnel_url にしてください。"
  fi
  print "⚠️ 公開 URL が変わりました。ChatGPT のプラグイン設定で URL を $tunnel_url/mcp に更新してください。"
fi

if ! listening "$mcp_port"; then
  (cd /tmp && nohup "$py" -m symnote.mcp_remote >"$logs/mcp-remote.log" 2>&1 &)
fi

sleep 3
print "SymNote:   http://localhost:$app_port"
print "ChatGPT 用: $(grep -E '^SYMNOTE_MCP_PUBLIC_URL=' "$env_file" | cut -d= -f2-)/mcp"
