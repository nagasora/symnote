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
- LLM: **Gemini 2.0 Flash-Lite**
- 追加ライブラリ: google-generativeai, python-dotenv, pandas, numpy ほか（`requirements.txt` 参照）

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

## Docker での起動
```bash
cp .env.example .env        # DB_PATH は /app/symnote.db 等に変更推奨
docker compose up --build
```
`http://localhost:8501` にアクセスするとアプリが表示されます。

## Google Calendar 連携

期限のある未完了タスクは、Google Calendar に期限日の 09:00（既定）から 30 分の予定として自動同期できます。タスクの保存・編集・完了・削除はローカル SQLite に先に記録されるため、オフライン時も失われず、次回アプリ起動時または「今すぐ同期」で再試行されます。Google の認証トークンは SQLite や `.env` には保存せず、OS の資格情報ストアに保存します。

1. Google Cloud Console で Google Calendar API を有効にし、**Desktop app** 用 OAuth クライアントを作成する。
2. ダウンロードしたクライアント JSON の絶対パスを `.env` の `GOOGLE_CALENDAR_CLIENT_SECRET_PATH` に設定する。
3. アプリのサイドバーから「Google Calendar」を開き、「Google Calendar に接続」を選ぶ。

`GOOGLE_CALENDAR_ID` は対象カレンダー（既定: `primary`）、`GOOGLE_CALENDAR_REMINDER_MINUTES` はポップアップ通知の分数（既定: `30`）です。時刻は `GOOGLE_CALENDAR_DEADLINE_HOUR`（既定: `9`）、タイムゾーンは `GOOGLE_CALENDAR_TIMEZONE`（既定: `Asia/Tokyo`）で変更できます。通知を受け取る端末では Google Calendar アプリまたはブラウザの通知を許可してください。

接続済みの Google Calendar には、毎朝 08:00（既定）に「今日が期限の未完了タスク」と「期限切れのやり残し」をまとめる予定も作成されます。`GOOGLE_CALENDAR_MORNING_DIGEST_HOUR` と `GOOGLE_CALENDAR_MORNING_DIGEST_LOOKAHEAD_DAYS`（既定: `31`）で時刻と同期対象期間を変更できます。SymNote は同期成功時点で今後の予定を更新するため、PC が停止中でもスマホの Google Calendar は最後に同期された内容を通知します。Google Calendar アプリ側の通知を許可してください。

## AI 連携（MCP サーバー）

SymNote は MCP サーバーとして、Claude Desktop / Claude Code などの AI からタスク・メモを読み書きできます。AI に「Gmail と Slack から今週の ToDo を探して SymNote に提案して」と頼むと、AI は自身のコネクタで情報源を読み、`propose_tasks` で **承認待ちの候補** を登録します。候補はアプリの「📥 候補の承認」画面で確認・手直ししてから、承認したものだけがタスクになります（メール本文などに紛れた指示で勝手にタスクが作られるのを防ぐため）。

| ツール | 用途 |
| --- | --- |
| `list_tasks` / `search_notes` / `list_task_candidates` | 既存のタスク・メモ・候補の確認（重複提案の回避） |
| `propose_tasks` | 外部の情報源から見つけた ToDo を候補として登録（同じ出典・タイトルは自動で重複排除、1 回 20 件まで） |
| `add_task` / `complete_task` | 会話の中でユーザー本人が明示的に依頼したときの直接登録・完了 |

承認・削除のツールは公開していません。

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

## 繰り返しタスク

タスク登録時に「なし」「毎日」「毎週」「隔週」を選べます。繰り返しタスクを完了にすると、完了履歴を残したまま次回分が自動作成されます。各回は通常のタスクとして Google Calendar に同期されるため、予定や通知の状態も混ざりません。タスク編集画面から「繰り返しを停止」を選ぶと、以降の自動作成だけを止められます。

## ディレクトリ
```
docs/                # 要件・画面仕様・データ仕様
src/symnote/         # Streamlit UI ＋ core ロジック
  ├─ app.py          # タブ UI
  ├─ calendar_app.py # 月カレンダー
  ├─ mcp_server.py   # MCP サーバー（AI 連携）
  ├─ views/          # 画面単位の UI（候補の承認など）
  ├─ core/db.py      # SQLite アクセス
  ├─ core/candidates.py # AI が提案した ToDo 候補の承認フロー
  ├─ core/nlp.py     # AI-1 & AI-2 & AI-3
  └─ core/weekly_review.py # (Deprecated)
tests/               # pytest
docker/              # Dockerfile / entrypoint
```

