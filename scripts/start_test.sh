#!/bin/zsh
# テスト環境を起動する：本番 DB の複製を使い、パソコンとスマホ（同じ Wi-Fi）から開ける。
# 本番（8510）とは別ポート・別 DB で動き、Google カレンダーへは書き込まない。
#   scripts/start_test.sh          起動（前回のテスト用 DB を引き継ぐ）
#   scripts/start_test.sh --reset  本番から複製し直して起動
#   scripts/start_test.sh --remote 外出先（モバイル回線）からも開ける HTTPS の URL を発行して起動
#   scripts/start_test.sh --stop   停止（公開 URL も閉じる）
set -u
# 本番 DB の複製・ログ・PID ファイルを他のユーザーから読めないようにする。
umask 077
root="${0:A:h:h}"
py="$root/.venv/bin/python"
env_file="$root/src/symnote/.env"
logs="$HOME/Library/Logs/SymNote"
port=8520
test_dir="$root/data/test"
test_db="$test_dir/symnote.test.db"
passcode_file="$test_dir/passcode"
tunnel_pid_file="$test_dir/tunnel.pid"
tunnel_log="$logs/cloudflared-test.log"
prod_db="$(grep -E '^DB_PATH=' "$env_file" 2>/dev/null | cut -d= -f2-)"
prod_db="${prod_db:-$root/symnote.db}"
mkdir -p "$logs" "$test_dir"

pid="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null)"
stop_server() {
  kill $pid
  # 終了を待たずに次の起動を確認すると、終了途中の旧プロセスを「起動済み」と誤認する。
  for _ in {1..20}; do
    lsof -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 || break
    sleep 0.5
  done
  pid=""
}
tunnel_pid() {
  local saved
  saved="$(cat "$tunnel_pid_file" 2>/dev/null)"
  # PID は再利用されるため、本番の MCP 用トンネルや無関係のプロセスを止めないよう中身を確かめる。
  [[ -n "$saved" ]] && ps -p "$saved" -o command= 2>/dev/null | grep -q "cloudflared.*localhost:$port" && print "$saved"
}
stop_tunnel() {
  local tpid
  tpid="$(tunnel_pid)"
  [[ -n "$tpid" ]] && kill "$tpid" && print "公開 URL を閉じました。"
  rm -f "$tunnel_pid_file"
}
read_tunnel_url() {
  "$py" -c 'import sys; sys.path.insert(0, sys.argv[1]); from prepare_test_env import parse_tunnel_url; print(parse_tunnel_url(open(sys.argv[2]).read()) or "")' \
    "$root/scripts" "$tunnel_log" 2>/dev/null
}
# DNS で確かめると、発行直後の「まだ無い」応答が Mac にキャッシュされ数十分開けなくなるため、ログで判定する。
tunnel_alive() {
  [[ "$("$py" -c 'import sys; sys.path.insert(0, sys.argv[1]); from prepare_test_env import tunnel_is_registered; print(int(tunnel_is_registered(open(sys.argv[2]).read())))' \
    "$root/scripts" "$tunnel_log" 2>/dev/null)" == 1 ]]
}
start_tunnel() {
  local tpid url
  tpid="$(tunnel_pid)"
  # quick tunnel は Mac のスリープや回線の切り替えで Cloudflare 側から消され、
  # プロセスは生きたまま再接続を繰り返す。古い URL を見せないよう、届かなければ作り直す。
  if [[ -n "$tpid" ]] && ! tunnel_alive; then
    stop_tunnel >/dev/null
    tpid=""
  fi
  if [[ -z "$tpid" ]]; then
    : >"$tunnel_log"
    nohup cloudflared tunnel --no-autoupdate --url "http://localhost:$port" >"$tunnel_log" 2>&1 &
    print $! >"$tunnel_pid_file"
  fi
  for _ in {1..40}; do
    url="$(read_tunnel_url)"
    [[ -n "$url" ]] && break
    sleep 0.5
  done
  for _ in {1..30}; do
    [[ -z "$url" ]] || tunnel_alive && break
    sleep 1
  done
  print "$url"
}

if [[ "${1:-}" == "--stop" ]]; then
  stop_tunnel
  if [[ -n "$pid" ]]; then stop_server; print "テスト環境を停止しました。"; else print "テスト環境は動いていません。"; fi
  exit 0
fi
remote_flag=()
if [[ "${1:-}" == "--remote" ]]; then
  if ! command -v cloudflared >/dev/null; then
    print "cloudflared が見つかりません。brew install cloudflared で入れてください。"
    exit 1
  fi
  remote_flag=(--remote)
fi
reset_flag=()
if [[ "${1:-}" == "--reset" ]]; then
  reset_flag=(--reset)
  # 動作中のまま DB を差し替えると、開いている接続が古いファイルを掴み続ける。
  [[ -n "$pid" ]] && stop_server
fi

show_urls() {
  local url_args=()
  if (( ${#remote_flag} )); then
    local url
    url="$(start_tunnel)"
    if [[ -z "$url" ]]; then
      print "公開 URL の発行に失敗しました。ログ: $tunnel_log"
    else
      url_args=(--remote-url "$url")
    fi
  elif [[ -n "$(tunnel_pid)" ]]; then
    local url
    url="$(read_tunnel_url)"
    if tunnel_alive; then
      url_args=(--remote-url "$url")
    else
      print "前回の公開 URL は切れています。外出先から開くには --remote で発行し直してください。"
    fi
  fi
  "$py" "$root/scripts/prepare_test_env.py" --source "$prod_db" --target "$test_db" \
    --passcode-file "$passcode_file" --port "$port" --show-only "${url_args[@]}"
}

if [[ -n "$pid" ]]; then
  print "テスト環境はすでに動いています。"
  show_urls
  exit 0
fi

"$py" "$root/scripts/prepare_test_env.py" --source "$prod_db" --target "$test_db" \
  --passcode-file "$passcode_file" --port "$port" "${reset_flag[@]}" >/dev/null || exit 1

# パスコード画面の前でも Streamlit のアップロード API は届くため、1 回の大きさを画面側の上限（25MB）に絞る。
(cd "$root" && DB_PATH="$test_db" SYMNOTE_TEST_MODE=1 SYMNOTE_APP_PASSCODE="$(cat "$passcode_file")" \
  PYTHONPATH="$root/src" nohup "$py" -m streamlit run src/symnote/app.py \
  --server.address 0.0.0.0 --server.port "$port" --server.headless true --server.maxUploadSize 25 \
  --browser.gatherUsageStats false >"$logs/streamlit-test.log" 2>&1 &)

for _ in {1..20}; do
  lsof -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 && break
  sleep 0.5
done
if lsof -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
  print "テスト環境を起動しました（停止: scripts/start_test.sh --stop）。"
  print "初回は macOS が「受信接続を許可しますか？」と聞くことがあります → 許可してください。"
  show_urls
else
  print "起動に失敗しました。ログ: $logs/streamlit-test.log"
  exit 1
fi
