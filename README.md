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
# LLM_MODEL=gemini-2.0-flash-lite (デフォルト)

streamlit run src/symnote/app.py
```

## Docker での起動
```bash
cp .env.example .env        # DB_PATH は /app/symnote.db 等に変更推奨
docker compose up --build
```
`http://localhost:8501` にアクセスするとアプリが表示されます。

## ディレクトリ
```
docs/                # 要件・画面仕様・データ仕様
src/symnote/         # Streamlit UI ＋ core ロジック
  ├─ app.py          # タブ UI
  ├─ calendar_app.py # 月カレンダー
  ├─ core/db.py      # SQLite アクセス
  ├─ core/nlp.py     # AI-1 & AI-2 & AI-3
  └─ core/weekly_review.py # (Deprecated)
tests/               # これから整備
docker/              # Dockerfile / entrypoint
```

