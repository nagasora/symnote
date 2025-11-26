# SymNote 🧠

思考インボックスからタスク優先づけ、週次振り返りまでを Streamlit で実現する AI ノートアプリです。  
メモを書くだけで AI が分類・優先順位付けを行い、今日やるべき 3 件と今週のフォーカスを提案します。

## 主な機能
- 📝 インボックス: なんでも書き込んで AI が `task / idea / someday` に自動分類
- 🗂️ タスク整理: 重要度/緊急度/所要時間などを調整しつつ、期限順にタスクを俯瞰
- 📅 今日ビュー: AI が「今日のトップ3」を推薦し、理由とステータス更新をサポート
- 🌤 今週ビュー: 今週タスクと「AI 週次レポート」を表示、ワンクリックで DB に保存
- 🗓 カレンダー: 週単位でタスク/メモ件数を確認し、特定日を掘り下げ

## AI 機能（MVP）
| ID  | 機能 | 内容 |
| --- | --- | --- |
| AI-1 | メモ分類 | ルールベースで `task/idea/someday` を判定し、importance/urgency/effort/energy と初期ステータスを推定 |
| AI-2 | 今日のタスク提案 | status ∈ {inbox, week, today} のタスクから、期限・優先度・所要時間を考慮してトップ3を推薦し、理由を提示 |
| AI-3 | 週次振り返り | 期間内の完了/未完タスクとメモを集計し、「サマリー/良かった点/学び/来週フォーカス」を生成。Markdown を `items` テーブルへ保存 |

## 環境
- Python 3.12
- Streamlit 1.39
- SQLite (シングルテーブル `items`)
- 追加ライブラリ: SQLAlchemy, pandas, numpy ほか（`requirements.txt` 参照）

## 開発環境（ローカル）
```bash
git clone <repo>
cd symnote

python -m venv .venv
. .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env        # OPENAI_API_KEY 等を必要に応じて設定

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
  ├─ calendar_app.py # 週カレンダー
  ├─ core/db.py      # SQLite アクセス
  ├─ core/nlp.py     # AI-1 & AI-2
  └─ core/weekly_review.py # AI-3
tests/               # これから整備
docker/              # Dockerfile / entrypoint
```

## 今後のTODO
- sentence-transformers / OpenAI API を活用した embeddings & 要約
- テストコード整備（pytest）
- マルチユーザー/共有、外部カレンダー連携
