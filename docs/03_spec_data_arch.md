# データ仕様 & アーキテクチャ v0.1

## 1. DB スキーマ

テーブル: `notes`

```sql
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,  -- ISO文字列
  date TEXT NOT NULL,        -- "YYYY-MM-DD"（論理的な日付キー）
  title TEXT,
  raw_text TEXT NOT NULL,
  summary TEXT,
  keywords TEXT,             -- JSON ["...", "..."]
  tags TEXT,                 -- "会議,研究" のようなカンマ区切り
  kind TEXT NOT NULL,        -- "note" | "todo" | "event" | "weekly_review"
  embedding BLOB             -- np.float32 正規化ベクトル
);
CREATE INDEX IF NOT EXISTS idx_notes_date ON notes(date);
CREATE INDEX IF NOT EXISTS idx_notes_kind ON notes(kind);
