# SymNote 🧠

思考インボックスからタスク優先づけまでを Streamlit で実現する AI ノートアプリです。  
メモを書くだけで AI が分類・優先順位付けを行い、今日やるべき 3 件を提案します。

## 主な機能
- 📝 インボックス: なんでも書き込んで AI が `task / idea / someday` に自動分類。期限設定や「メモ/タスク」の明示的な保存も可能。
- 🗂️ タスク整理: 重要度/緊急度/所要時間などを調整しつつ、期限順にタスクを俯瞰
- 📅 今日ビュー: AI が「今日のトップ3」を推薦し、理由とステータス更新をサポート。期限切れタスクはアラート表示。
- 🗓 カレンダー: 月単位でタスク/メモ件数を確認し、特定日を掘り下げて詳細を表示
- 💡 アイデア整理: 実現したいことを書くと AI が具体的な ToDo リストを生成

## AI 機能（MVP）
| ID  | 機能 | 内容 |
| --- | --- | --- |
| AI-1 | メモ分類 | ルールベースで `task/idea/someday` を判定し、importance/urgency/effort/energy と初期ステータスを推定 |
| AI-2 | 今日のタスク提案 | status ∈ {inbox, week, today} のタスクから、期限・優先度・所要時間を考慮してトップ3を推薦し、理由を提示 |
| AI-3 | アイデアToDo化 | 漠然としたアイデアや目標を入力すると、Gemini 2.0 Flash-Lite が具体的なアクションプラン（ToDo）を生成 |

## 環境
- Python 3.12
- Streamlit 1.39
- SQLite (シングルテーブル `items`)
- LLM: **Gemini**（`LLM_MODEL` で指定、モデル未検出時は `gemini-flash-latest` に切り替え）
- 追加ライブラリ: google-genai, python-dotenv, pandas, numpy ほか（`requirements.txt` 参照）

## 開発環境（ローカル）
```bash
git clone <repo>
cd symnote

python -m venv .venv
. .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env        # LLM_API_KEY (Gemini) を設定
# LLM_MODEL=gemini-3.1-flash-lite (デフォルト)

streamlit run src/symnote/app.py
```

## テスト環境（パソコン・スマホで試す）

本番（`http://localhost:8510`）とは別に、本番 DB の複製で動くテスト環境を起動できます。

```bash
scripts/start_test.sh           # 起動（前回のテスト用 DB を引き継ぐ）
scripts/start_test.sh --reset   # 本番から複製し直して起動
scripts/start_test.sh --remote  # 外出先（モバイル回線）からも開ける HTTPS の URL を発行して起動
scripts/start_test.sh --stop    # 停止（公開 URL も閉じる）
```

- Finder から `SymNoteテスト環境.command` をダブルクリックしても起動できます。
- パソコンは `http://localhost:8520`。スマホは同じ Wi-Fi に接続し、表示された `http://<MacのIP>:8520` を開きます（端末に QR コードも出ます）。
- `--remote` は cloudflared のクイックトンネルで `https://〜.trycloudflare.com` を発行します。Mac が起動している間だけ、Wi-Fi 以外（モバイル回線）のスマホからも開けます。URL は起動のたびに変わり、本番の MCP 用トンネルとは別です。
- 画面を開くにはパスコード（`data/test/passcode`）が必要です。5 回間違えると 15 分間入力できなくなります。
- テスト環境での変更は本番に反映されません。Google カレンダーは読むだけで、書き出し・同期・接続の解除はしません。

## Docker での起動
```bash
cp .env.example .env        # DB_PATH は /app/symnote.db 等に変更推奨
docker compose up --build
```
`http://localhost:8501` にアクセスするとアプリが表示されます。

## Google Calendar 連携

接続すると、Google Calendar に **毎朝 1 件の「朝のまとめ」予定**（既定 08:00、15 分、開始時にポップアップ通知）を作成します。内容は「今日が期限の未完了タスク」と「期限切れのやり残し」です。タスクごとの予定は作成しません（旧バージョンが作ったタスク別の予定は、同期時に自動で削除されます）。

- タスクはローカル SQLite が正で、Calendar へは一方向に書き出すだけです。オフラインでもタスク操作は失われず、次回の同期で反映されます。
- まとめ予定の ID は日付から決まるため、通信断の後に再試行しても予定は重複しません。
- `GOOGLE_CALENDAR_ID` を変更すると、旧カレンダーの当日分を削除して新しいカレンダーに作り直します。
- 「今日」は `GOOGLE_CALENDAR_TIMEZONE`（既定: `Asia/Tokyo`）の日付で判定します。
- Google の認証トークンは SQLite や `.env` には保存せず、OS の資格情報ストアに保存します。

設定手順:

1. Google Cloud Console で Google Calendar API を有効にし、**Desktop app** 用 OAuth クライアントを作成する。
2. ダウンロードしたクライアント JSON の絶対パスを `.env` の `GOOGLE_CALENDAR_CLIENT_SECRET_PATH` に設定する。
3. アプリのサイドバーから「Google Calendar」を開き、「Google Calendar に接続」を選ぶ。
4. 通知を受け取る端末（スマホ等）で Google Calendar アプリの通知を許可する。

通知時刻は `GOOGLE_CALENDAR_MORNING_DIGEST_HOUR`（既定: `8`）で変更できます。タスク登録画面の期限時刻の初期値は `GOOGLE_CALENDAR_DEADLINE_HOUR`（既定: `9`）です。

### アプリを開かずに毎朝同期する（macOS / launchd）

まとめ予定は同期した時点の内容で作られます。アプリを開かない日も通知が届くよう、同期コマンドを定期実行してください。

```bash
DB_PATH=/absolute/path/to/symnote.db PYTHONPATH=/absolute/path/to/symnote/src \
  /absolute/path/to/symnote/.venv/bin/python -m symnote.calendar_sync
```

`docs/launchd/com.symnote.calendar-sync.plist.example` のパスを書き換えて `~/Library/LaunchAgents/com.symnote.calendar-sync.plist` に置き、`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.symnote.calendar-sync.plist` で登録します。30 分ごとに実行され、内容が変わらなければ Google には接続しません。スリープ中に過ぎた実行は復帰時に行われます（Mac が終日停止している日は通知されません）。

## AI 連携（MCP サーバー）

SymNote は MCP サーバーとして、Claude Desktop / Claude Code などの AI からタスク・メモを読み書きできます。AI に「Gmail と Slack から今週の ToDo を探して SymNote に提案して」と頼むと、AI は自身のコネクタで情報源を読み、`propose_tasks` で **承認待ちの候補** を登録します。候補はアプリの「📥 候補の承認」画面で確認・手直ししてから、承認したものだけがタスクになります（メール本文などに紛れた指示で勝手にタスクが作られるのを防ぐため）。

| ツール | 用途 |
| --- | --- |
| `list_tasks` / `search_notes` / `list_task_candidates` | 既存のタスク・メモ・候補の確認（重複提案の回避） |
| `propose_tasks` | 外部の情報源から見つけた ToDo を候補として登録（同じ出典・タイトルは自動で重複排除、1 回 20 件まで） |
| `add_task` / `complete_task` | 直接のタスク登録・完了（**既定では無効**。下記参照） |

承認・削除のツールは公開していません。`add_task` / `complete_task` は、外部情報を読む AI がメール本文などの指示に従って承認を迂回できないよう、既定では公開しません。チャットから直接追加・完了したい場合は、`env` に `"SYMNOTE_MCP_DIRECT_WRITES": "1"` を加えた **別名の接続**（例: `symnote-write`）を登録し、メール等を読ませる作業ではその接続を有効にしないでください。
インターネットに公開するリモート接続（ChatGPT / claude.ai）はこの設定の影響を受けません。公開接続で直接書き込みを許す場合だけ、別の `SYMNOTE_MCP_REMOTE_DIRECT_WRITES=1` を設定します（非推奨）。

```bash
uv pip install --python .venv/bin/python "mcp>=1.20,<2"
```

Claude Desktop の `claude_desktop_config.json`（Claude Code では `.mcp.json`）に次を追加します。`DB_PATH` は **絶対パス** で指定してください（MCP クライアントは任意の作業ディレクトリでサーバーを起動するため、相対パスだと起動を拒否します）。

```json
{
  "mcpServers": {
    "symnote": {
      "command": "/absolute/path/to/symnote/.venv/bin/python",
      "args": ["-m", "symnote.mcp_server"],
      "env": {
        "PYTHONPATH": "/absolute/path/to/symnote/src",
        "DB_PATH": "/absolute/path/to/symnote.db"
      }
    }
  }
}
```

Streamlit アプリと MCP サーバーは同じ SQLite ファイルを同時に使えます（WAL モード）。

### claude.ai から使う（リモート接続）

claude.ai（Web / スマホアプリ）は Anthropic のクラウドから接続してくるため、Mac 上の SymNote を HTTPS で公開します。`symnote.mcp_remote` は 127.0.0.1 だけで待ち受け、Cloudflare Tunnel がそこへ中継します。接続時は OAuth の同意画面が開き、**パスフレーズを入力した場合だけ** claude.ai にトークンが発行されます。公開するツールはローカル接続と同じです（`add_task` / `complete_task` は既定で無効）。

1. `.env` に設定を追加します（`.env.example` 参照）。

   ```bash
   SYMNOTE_MCP_PUBLIC_URL=https://symnote.example.com   # トンネルの公開 URL（パスなし）
   SYMNOTE_MCP_PASSPHRASE=十分に長いパスフレーズ           # 12 文字以上
   ```

2. サーバーを起動します（常駐させるなら `docs/launchd/com.symnote.mcp-remote.plist.example` を使います）。

   ```bash
   DB_PATH=/absolute/path/to/symnote.db PYTHONPATH=src .venv/bin/python -m symnote.mcp_remote
   ```

3. Cloudflare Tunnel で `http://127.0.0.1:8765` を公開します（`brew install cloudflared`）。
   - **独自ドメインを Cloudflare で管理している場合（推奨）**: URL が固定されます。
     ```bash
     cloudflared tunnel login
     cloudflared tunnel create symnote
     cloudflared tunnel route dns symnote symnote.example.com
     cloudflared tunnel run --url http://127.0.0.1:8765 symnote
     ```
   - **ドメインがない場合（お試し）**: `cloudflared tunnel --url http://127.0.0.1:8765` で表示される `https://〜.trycloudflare.com` を `SYMNOTE_MCP_PUBLIC_URL` に設定してサーバーを再起動します。cloudflared を再起動するたびに URL が変わり、コネクタの登録し直しが必要です。

4. claude.ai の「設定 → コネクタ → カスタムコネクタを追加」で、URL に `https://symnote.example.com/mcp` を入力します（OAuth の Client ID / Secret 欄は空のまま）。「連携」を押すと SymNote の同意画面が開くので、パスフレーズを入力して承認します。

- 承認後の戻り先は claude.ai / claude.com とローカルホストだけに制限しています。ほかのサービスを使う場合は `SYMNOTE_MCP_REDIRECT_URIS`（カンマ区切り）で戻り先を追加します。
- 同じ送信元からパスフレーズを 5 回間違えるとその送信元を 15 分間ロックし、全体で 50 回失敗すると全体を 15 分間ロックします。
- アクセスが大量に来る場合は、Cloudflare 側のレート制限（WAF）も併用してください。
- アクセストークンの有効期限は 1 時間、リフレッシュトークンは 30 日（使うたびに更新）です。使用済みのリフレッシュトークンが再び使われたら盗用とみなし、その接続を無効にします（claude.ai で再接続が必要になります）。トークンは DB にハッシュだけを保存します。
- パスフレーズが漏れた・端末を手放したなどの場合は、パスフレーズを変えてから `python -m symnote.mcp_remote --revoke-all` で全接続を無効にします。

## 繰り返しタスク

タスク登録時に「なし」「毎日」「毎週」「隔週」を選べます。繰り返しタスクを完了にすると（編集画面でステータスを done にした場合も含め）、完了履歴を残したまま次回分が自動作成されます。終了日を空欄にすると無期限に繰り返します。タスク編集画面から「繰り返しを停止」を選ぶと、以降の自動作成だけを止められます。

## ディレクトリ
```
docs/                # 要件・画面仕様・データ仕様
src/symnote/         # Streamlit UI ＋ core ロジック
  ├─ app.py          # タブ UI
  ├─ calendar_app.py # 月カレンダー
  ├─ mcp_server.py   # MCP サーバー（AI 連携）
  ├─ mcp_remote.py   # claude.ai 向けリモート MCP（HTTP + OAuth）
  ├─ calendar_sync.py # Google Calendar 定期同期コマンド
  ├─ views/          # 画面単位の UI（候補の承認など）
  ├─ core/db.py      # SQLite アクセス
  ├─ core/candidates.py # AI が提案した ToDo 候補の承認フロー
  ├─ core/oauth_store.py # リモート MCP の OAuth クライアント・トークン保存
  ├─ core/nlp.py     # AI-1 & AI-2 & AI-3
  └─ core/weekly_review.py # (Deprecated)
tests/               # pytest
docker/              # Dockerfile / entrypoint
```

