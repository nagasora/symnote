# データ仕様 & アーキテクチャ v0.1

---

## 1. 目標
SymNote は「思考インボックス & タスク優先づけ」に特化する。  
データ構造は **メモ・タスク・振り返りをすべて `items` に集約** してシンプルに運用する。

---

## 2. DB スキーマ（MVP）
### `items` テーブル
```sql
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY,
  created_at TEXT,
  date TEXT,
  kind TEXT,              -- 'memo' | 'task' | 'weekly_review'
  raw_text TEXT,
  ai_category TEXT,       -- 'task' | 'idea' | 'someday'
  importance INTEGER,     -- 1-5
  urgency INTEGER,        -- 1-5
  effort TEXT,            -- 'short' | 'medium' | 'long'
  energy TEXT,            -- 'low' | 'mid' | 'high'
  status TEXT,            -- 'inbox' | 'today' | 'week' | 'done'
  tags TEXT,              -- comma separated
  embedding BLOB
);

CREATE INDEX IF NOT EXISTS idx_items_date ON items(date);
CREATE INDEX IF NOT EXISTS idx_items_kind ON items(kind);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
```

---

## 3. データフロー（MVP）
1. **インボックス → AI 分類 → タスク化**  
   `raw_text -> classify -> (kind, ai_category, importance, urgency, effort, energy, status)` → `items` 保存
2. **タスク整理画面**  
   ユーザーが `importance/urgency/effort/energy/status` を調整し、`items` を更新
3. **今日 / 今週ビュー**  
   `status='today' | 'week'` のタスクを表示、完了ボタンで `status='done'`
4. **週次振り返り生成**  
   指定期間の `kind in ('task','memo')` を収集 → AI に渡し `kind='weekly_review'` として保存

---

## 4. モジュール構成（MVP）
```
src/symnote/
├─ app.py               # Streamlit UI（タブ）
├─ calendar_app.py      # 週カレンダー UI
├─ config.py            # .env 読み込み
└─ core/
    ├─ db.py            # SQLite アクセス
    ├─ nlp.py           # AI 分類・優先度計算
    ├─ weekly_review.py # 週次振り返り生成
    └─ priority.py      # （将来）高度な優先度計算を切り出し
```

---

## 5. AI 設計（MVP）
### 5.1 AI-1 メモ分類
**目的**: インボックスに入ったテキストを `task/idea/someday` に分類し、タスクであれば優先度属性とステータス初期値を付与する。

**入力**: `raw_text`, `tags`, `created_at`, 手動で指定された `date`（任意）  
**出力**: `ClassificationResult`（ai_category, importance, urgency, effort, energy, status_suggestion）

**アルゴリズム**
1. **前処理**:  
   - 小文字化、句読点除去  
   - 正規表現で日時表現を抽出（`今日`, `明日`, `今週`, `dead line`, `YYYY/MM/DD` など）  
   - キーワード辞書（deadline, idea, someday, research, meeting, etc.）を参照
2. **カテゴリ判定**:
   - `idea` キーワード（アイデア、構想、maybe）を含む → idea  
   - `someday` キーワード（いつか、someday）を含む → someday  
   - それ以外は task
3. **スコアリング**:
   - importance: 締切に関連するキーワードがあれば +1、研究/キャリア関連キーワードで +1、デフォルト 3  
   - urgency: 今日/明日/ASAP などで +2、来週/来月で -1、デフォルト 2  
   - effort: テキスト長 < 40 → short、<120 → medium、それ以上 → long  
   - energy: 「設計/企画/書く」などで high、「連絡/整理/メール」で low、それ以外 mid
4. **ステータス推定**: importance + urgency >= 8 → today、>=6 → week、その他 inbox
5. **DB への反映**:  
   - `kind = 'task'`（ai_category=task のとき）  
   - `status = status_suggestion`  
   - idea/someday は `kind='memo'` のままにする

**実装インターフェース**:  
`classify_text_rule_based(text: str) -> ClassificationResult`（既存）を拡張し、辞書・ルールを別ファイルに切り出せるようにする。

### 5.2 AI-2 今日のタスク提案
**目的**: `status in ('today','week','inbox')` のタスクから「今日やるべき 3 件」を推薦し、理由を提示する。

**入力**: `tasks`（kind='task' で status != 'done' のレコード群）  
**出力**: `[TodaySuggestion]`（task_id, reason, score, recommended_status）

**アルゴリズム**
1. **候補抽出**:  
   - `status` in ('today','week','inbox') のタスク  
   - 今日の日付より過去の `date` が設定されているものは優先
2. **スコア計算**:  
   ```
   base = importance * urgency
   effort_penalty = {short:1.0, medium:0.9, long:0.75}
   energy_bonus = {low:0.95, mid:1.0, high:1.05}
   overdue_bonus = 1.2 if date < today else 1.0
   carry_over_bonus = 1.1 if status='week' and last_updated < today
   score = base * effort_penalty * energy_bonus * overdue_bonus * carry_over_bonus
   ```
3. **説明生成**:  
   - 期限が近い → `due_reason`  
   - 重要度/緊急度が高い → `priority_reason`  
   - effort が短い → `quick_win_reason`  
   - これらをテンプレ化して `reason` を構築
4. **推薦**:  
   - スコア順に並べ上位 3 件を返す  
   - 既に `status='today'` でないものは `status='today'` へ更新提案（UI で受け入れ）

**実装インターフェース案**:  
`select_today_tasks(tasks: list[ItemRow], today: date) -> list[TodaySuggestion]`

### 5.3 AI-3 週次振り返りジェネレーター
**目的**: 週内の完了タスクとメモをもとに、要約/良かった点/次週フォーカスを自動生成する。

**入力**: `tasks`（期間内の kind='task'）と `memos`（kind='memo'）  
**出力**: `WeeklyReview`（period, summary, good_points, learnings, focus_next）

**アルゴリズム**
1. **集計**:  
   - 完了タスク件数、未完タスク件数（today/week）、メモ件数  
   - カテゴリ別（idea/someday/task）やタグ別のトップ出現も集計
2. **テンプレ構築**:  
   - 今週の要約 = 完了数/未完数/メモ数を用いた文章  
   - 良かった点 = 完了タスク中の high importance や継続的作業を列挙  
   - 改善点/学び = 未完タスクや溜まったメモ数から推測（例: 未完 > 完了 → 「優先タスクに集中」）  
   - 次週フォーカス = 期限が近く未完のタスク、idea → task へ昇格すべきもの
3. **出力保存**:  
   - `kind='weekly_review'`, `raw_text` に markdown を格納  
   - `items` テーブルに新規行を追加し、UI の「週次振り返り」タブで表示

**実装インターフェース案**:  
`generate_weekly_review(today: date | None = None) -> WeeklyReviewPayload`

---

## 6. 今後の拡張
- `events` テーブルを追加し予定管理と連携
- カレンダーとの双方向同期（Google Calendar 等）
- Supabase / Postgres への移行
- チーム共有（ユーザーごとのスキーマ拡張）
