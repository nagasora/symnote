# SymNote UI 刷新 実装計画

- 作成日: 2026-10-04
- 決定事項: 画面を作り直す（方式 B: FastAPI ＋ React）／ デザインの方向性②「やわらかい紙のノート」／ PC とスマホを同じ比重で使う
- 実装担当: Codex（このドキュメントの PR 単位で依頼する）

---

## 0. このドキュメントの使い方

| 資料 | 場所 | 用途 |
|---|---|---|
| 実装計画（本書） | `docs/redesign/PLAN.md` | 何をどの順で作るか。**モックと食い違う場合は本書を優先** |
| 画面モック | `docs/redesign/mockups/index.html` | 見た目と動きの正解。ブラウザで開いて操作できる |
| スクリーンショット | `docs/redesign/mockups/screens/*.png` | PC・タブレット・スマホ・ダークの完成イメージ |
| デザイントークン | `docs/redesign/mockups/tokens.css` | 色・文字・余白・角丸・動き。**実装へそのまま移す** |

モックの開き方:

```bash
cd docs/redesign/mockups && python3 -m http.server 8611
# http://localhost:8611/            … 画面右上の「モック」で ライト/ダーク・⌘K を切り替え
# http://localhost:8611/?detail=1#all … 詳細パネルを開いた状態
# http://localhost:8611/?view=cal#all … カレンダー表示
# スマホ表示はブラウザの幅を 860px 以下にする
```

モックの CSS（`index.html` の `<style>`）はクラス名ごとに部品の仕様になっている。React 化するときは、同じ見た目になるよう **値をそのまま移植してよい**（`.task`、`.check`、`.ai-card`、`.cand`、`.props`、`.capture` など）。

---

## 1. 目的とゴール

### 1.1 今の問題（Streamlit 版）

1. Streamlit 標準の見た目（赤いラジオ・Deploy ボタン）で、個人の道具としての質感がない。
2. メニュー 8 項目が平坦で、毎日使う画面と設定が同列。
3. 数値や見出しの重複、絵文字の多用で、本当に見たい「今日のタスク」が下に押し出される。
4. 操作のたびに全画面を再描画し、完了するのに「開く → ボタン」の 2 手が必要。
5. スマホではまともに使えない。

### 1.2 ゴール

- **3 秒で今日やることが分かり、1 タップで完了できる。**
- **どこからでも 1 手で書き留められる**（PC は ⌘K、スマホは中央の ＋）。
- **AI の候補は受信箱で確認してから登録**（MCP の承認フローを UI の中心に据える）。
- PC（1440px）・タブレット（1024px）・スマホ（390px）で同じ機能が使え、スマホはホーム画面に追加してアプリのように使える（PWA）。
- ライト・ダーク両対応、WCAG AA のコントラスト、キーボード操作可能。

### 1.3 情報設計（8 画面 → 4 画面 ＋ 設定）

| 新しい画面 | 中身 | 旧画面 |
|---|---|---|
| **今日** | 今日のおすすめ（AI トップ3）／期限切れ／今日／今週のあとで／今日完了したもの | 今日 |
| **受信箱** | AI の候補（承認・却下）／書き留めたもの（あとで仕分け） | 候補の承認 |
| **すべて** | タスクとメモの一覧（絞り込み・検索）、表示切り替えでカレンダー | タスク、メモ、カレンダー |
| **ひらめき** | 資料から始めるセッション（要約・ToDo 抽出・マインドマップ・AI チャット） | アイデア整理、ドキュメント分析 |
| 設定 | テーマ／Google Calendar／AI 連携（MCP）／スマホから使う／データ | Google Calendar |

タスクの作成フォームは独立画面にせず、**クイック入力（⌘K / ＋）と詳細パネル**に統合する。

---

## 2. 守ること（必読）

`AGENTS.md` の制約に加え、この刷新で次を守る。

1. **DB と `.env` はユーザーの所有物。** テスト・E2E は必ず一時 DB（`tmp_path`／`DB_PATH` を一時ファイルに）を使う。実 DB を開く・移行する・消すコマンドを実行しない。
2. **マイグレーションは追加のみ。** `SCHEMA_VERSION` を 10 → 11 に上げ、列の追加だけ行う（§4.5）。v10 の DB で起動して壊れないことをテストする。
3. **ネットワークや LLM の API キーがなくても**、今日・受信箱・すべて・クイック入力・設定は完全に動く。AI を使う操作だけを無効化し、理由を表示する。
4. **MCP サーバー（`symnote.mcp_server`）と定期同期（`symnote.calendar_sync`）の挙動を変えない。** 既存テスト（94 件）は全て通ったままにする。共通化のため関数を `core/` に移すのは可、ただし外から見える結果は同じ。
5. **外部由来の文字列（候補のタイトル・抜粋・メモ本文）は常に文字列として描画する。** `dangerouslySetInnerHTML` は ESLint で禁止。Markdown 表示が必要になったら `react-markdown` ＋ `rehype-sanitize` で画像を無効化し、リンクは `http(s)` のみ許可。
6. **Streamlit 版は最終フェーズまで消さない。** 同じ DB を並行して使えること（SQLite WAL）。削除は別 PR で、ユーザーの了承後。
7. **秘密情報をリポジトリに入れない。** API トークンはユーザー設定ディレクトリに保存（§4.2）。

---

## 3. 全体アーキテクチャ

```
 ブラウザ（PC） / スマホ（PWA, Tailscale の HTTPS 経由）
        │  /api/*（JSON）  と  /（web/dist の静的ファイル）
        ▼
 FastAPI  symnote.api  ── 認証・入力検証・JSON 変換だけを担当
        │  直接呼び出し（同一プロセス）
        ▼
 symnote.core  … db / candidates / nlp / google_calendar / mindmap / doc_loader（既存＋追加）
        │
        ▼
 SQLite（DB_PATH） ← MCP サーバー・calendar_sync・Streamlit 版も同じ DB を使う
```

### 3.1 ディレクトリ構成（追加分）

```
src/symnote/
  api/
    __init__.py
    app.py            # create_app(): ルーター登録・静的配信・ミドルウェア
    server.py         # main(): uvicorn 起動（symnote-web コマンド）
    auth.py           # トークン生成・保存・検証、ログイン/ログアウト
    deps.py           # 認証依存性、設定の取得
    errors.py         # 例外 → {"error": {...}} 変換
    schemas.py        # Pydantic モデル（Task, Memo, Candidate, Today, ...）
    presenters.py     # DB 行 → スキーマ変換（title の決定など）
    sync.py           # 変更後の Calendar 同期を 1 本に直列化するワーカー
    routers/
      today.py  tasks.py  memos.py  capture.py  inbox.py
      calendar.py  search.py  ideas.py  settings.py  auth.py
  core/
    overview.py       # task_overview（app.py から移動）
    titles.py         # task_title（mcp_server から移動して共用）
    quick_parse.py    # クイック入力の日時・種類の推定（ルールベース）
    （db.py / candidates.py に関数追加）
web/
  package.json  vite.config.ts  tsconfig.json  index.html
  eslint.config.js  playwright.config.ts
  public/  icons/（192, 512, maskable, apple-touch-icon）
  src/
    main.tsx  App.tsx  routes.tsx
    styles/  tokens.css（mockups から移植）  globals.css
    api/     client.ts  schema.d.ts（自動生成）  queries/*.ts
    components/
      ui/      Button Chip Segmented IconButton Pips Toast Skeleton EmptyState Kbd
      layout/  AppShell Sidebar TabBar MobileHeader DetailPane BottomSheet
      task/    TaskRow TaskList TaskSection TaskDetail PropRow CheckButton
      capture/ CaptureDialog ParsedChips
    features/
      today/ inbox/ all/（ListView, CalendarView） ideas/ settings/ auth/
    lib/     dates.ts  keyboard.ts  theme.ts  useMediaQuery.ts  swipe.ts
  tests/     unit（Vitest） e2e（Playwright）
docs/launchd/com.symnote.web.plist.example
```

### 3.2 技術選定

| 領域 | 採用 | 理由 |
|---|---|---|
| API | FastAPI ＋ uvicorn ＋ Pydantic v2 ＋ python-multipart | `core/` をそのまま呼べる。OpenAPI から型を生成できる |
| ビルド | Vite ＋ TypeScript（strict） | 速い。PWA プラグインがある |
| UI | React 19 ＋ React Router | 情報量が多い。部品化と状態管理の資産が厚い |
| サーバー状態 | TanStack Query | 楽観的更新・再取得・フォーカス時更新を標準で持つ |
| スタイル | Tailwind CSS v4 ＋ `tokens.css` の CSS 変数 | 色・余白は変数経由に限定し、ダーク切り替えを 1 か所に集約 |
| 部品の土台 | Radix UI（Dialog, Popover, Tabs, ToggleGroup, DropdownMenu） | アクセシビリティ（フォーカス管理・ARIA）を自前で書かない |
| コマンドパレット | cmdk | ⌘K の入力＋検索 |
| ボトムシート | vaul | スマホの詳細・入力。ドラッグで閉じる |
| アニメーション | motion（旧 framer-motion） | 完了・承認の退場、シートの出入り |
| 日付 | date-fns（ja ロケール）＋ react-day-picker | 「10月4日（日）」表記と日付選択 |
| アイコン | lucide-react | モックのアイコンは lucide と同じ形 |
| フォント | @fontsource/shippori-mincho-b1, @fontsource/zen-kaku-gothic-new | 同梱してオフラインでも同じ見た目（モックは Google Fonts を読むが、本実装では外部読み込みしない） |
| PWA | vite-plugin-pwa | manifest とサービスワーカー |
| 型生成 | openapi-typescript | API と型のずれを CI で検出 |
| テスト | pytest（API）、Vitest ＋ Testing Library、Playwright | §7 |

Node は 20 以上、パッケージ管理は npm（`package-lock.json` をコミット）。Python 側は `pyproject.toml` に extras `web = ["fastapi", "uvicorn[standard]", "python-multipart"]` を追加し、`dev` にも含める。

---

## 4. バックエンド（API）

### 4.1 起動と配信

- コマンド `symnote-web`（`symnote.api.server:main`）。`SYMNOTE_HOST`（既定 `127.0.0.1`）、`SYMNOTE_PORT`（既定 `8765`）。
- 起動時に `is_absolute_db_path` で DB_PATH を検査し、相対パスなら終了コード 2（MCP・calendar_sync と同じ規則）。その後 `init_db()`。
- `/api/*` 以外のパスは `web/dist` を返し、存在しないパスは `index.html`（SPA のフォールバック）。`web/dist` が無ければ「`cd web && npm ci && npm run build` を実行してください」という HTML を返す。
- 開発時は Vite（5173）が `/api` を 8765 にプロキシする。
- エンドポイントは同期関数（`def`）で書く。SQLite が同期 API のため、FastAPI のスレッドプールで動かす。

### 4.2 認証とセキュリティ

スマホから Tailscale 経由で使うため、ローカルでも認証を必須にする（Tailscale serve は 127.0.0.1 に転送するので、接続元 IP では判別できない）。

1. **トークン**: 初回起動時に `secrets.token_urlsafe(32)` を生成し、`~/.config/symnote/api_token`（Windows は `%APPDATA%\symnote\`）に権限 600 で保存。環境変数 `SYMNOTE_API_TOKEN` があればそちらを優先。`.env` には書き込まない。
2. **ログイン**: 起動時のコンソールに `http://127.0.0.1:8765/login#token=…` を表示。ログイン画面は URL の `#token` を読んで `POST /api/auth/login` し、直後に `history.replaceState` でハッシュを消す（フラグメントはサーバーのログに残らない）。手入力欄も用意する。
3. **セッション**: ログイン成功で Cookie `symnote_session`（HttpOnly、SameSite=Strict、HTTPS なら Secure、有効期限 400 日）。値はトークンの HMAC。`Authorization: Bearer <token>` も受け付ける（スクリプト用）。比較は `secrets.compare_digest`。
4. **CSRF**: 状態を変えるメソッド（POST/PATCH/DELETE）は `X-SymNote: 1` ヘッダーを必須にする。CORS は許可しない（独自ヘッダーでプリフライトが必要になり、他サイトから送れない）。
5. **DNS リバインディング対策**: `TrustedHostMiddleware` で `localhost`、`127.0.0.1`、`*.ts.net`、`SYMNOTE_ALLOWED_HOSTS`（カンマ区切り）だけを許可。
6. 認証不要なのは `GET /api/health` と `POST /api/auth/login` のみ。ログイン失敗は 1 秒待ってから 401（総当たり対策）。
7. レスポンスヘッダー: `Content-Security-Policy: default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; connect-src 'self'`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: same-origin`。

### 4.3 データの形（API スキーマ）

既存の列の意味を変えずに、API では分かりやすい名前で出す。

```ts
type TaskStatus = "inbox" | "today" | "week" | "done";   // 画面表示: あとで / 今日 / 今週 / 完了

interface Task {
  id: number;
  kind: "task";
  title: string;          // items.tags。空なら本文の 1 行目、それも空なら「（無題 #id）」（MCP と同じ規則）
  notes: string;          // items.raw_text
  status: TaskStatus;
  due_date: string | null;   // YYYY-MM-DD
  due_time: string | null;   // HH:MM
  importance: number | null; // 1-5
  urgency: number | null;    // 1-5
  effort: "short" | "medium" | "long" | null;
  energy: "low" | "mid" | "high" | null;
  recurrence: { frequency: "daily" | "weekly"; interval_days: number; end_date: string | null; label: string } | null;
  source: { type: "gmail" | "slack" | "notion" | "chat" | "calendar" | "other"; candidate_id: number; url: string | null } | null;
  created_at: string;
  completed_at: string | null;   // v11 で追加
}

interface Memo { id: number; kind: "memo"; title: string; body: string; created_at: string; date: string | null; sorted: boolean; }

interface Candidate {           // task_candidates の行そのまま
  id: number; source: string; source_ref: string; source_url: string | null;
  title: string; details: string; excerpt: string;
  due_date: string | null; due_time: string | null; confidence: number | null;
  status: "pending" | "approved" | "rejected"; created_at: string; decided_at: string | null; task_id: number | null;
}

interface ApiError { error: { code: string; message: string } }   // 400/401/403/404/409/422
```

`title` は今の実装どおり `items.tags` に保存する（Streamlit 版・MCP と互換を保つため）。専用の `title` 列を作るのは本計画の範囲外。

### 4.4 エンドポイント一覧

| メソッド・パス | 内容 | 使う core 関数（★＝新規） |
|---|---|---|
| `GET /api/health` | 稼働確認・スキーマ版数 | — |
| `POST /api/auth/login` `POST /api/auth/logout` `GET /api/auth/me` | §4.2 | ★auth |
| `GET /api/today` | 今日画面に必要なもの一式（下記） | `fetch_tasks_for_today_view`, `suggest_today_tasks`, ★`task_overview`, ★`fetch_tasks_completed_on`, `fetch_tasks` |
| `GET /api/tasks?state=active\|done&due=overdue\|today\|week\|none&q=&limit=` | 一覧 | `fetch_tasks`, `search_items` |
| `POST /api/tasks` | 作成（繰り返し指定可） | `insert_task`, `create_recurring_task` |
| `GET /api/tasks/{id}` | 詳細 | ★`fetch_item`, ★`fetch_task_sources` |
| `PATCH /api/tasks/{id}` | 部分更新（title→tags, notes→raw_text） | `update_item_fields` |
| `POST /api/tasks/{id}/complete` | 完了。`{next_task_id, next_due_date}` を返す | `complete_task` |
| `POST /api/tasks/{id}/reopen` | 元に戻す。`{status, discard_next_task_id?}` | ★`reopen_task` |
| `POST /api/tasks/{id}/stop-recurrence` | 繰り返し停止 | `stop_task_recurrence` |
| `DELETE /api/tasks/{id}` | 削除 | `delete_item` |
| `GET /api/memos?q=` `POST /api/memos` `PATCH /api/memos/{id}` `DELETE /api/memos/{id}` | メモ | `fetch_memos`, `insert_memo`, `update_item_fields`, `delete_item` |
| `POST /api/capture/preview` | 入力中の文から種類・日時を推定（ネット不要） | ★`quick_parse.parse` |
| `POST /api/capture` | `{text, kind: "task"\|"memo"\|"later", title?, due_date?, due_time?}` で保存 | `insert_task`, `insert_memo`, ★`insert_unsorted` |
| `GET /api/inbox/counts` | `{candidates, unsorted}`（ナビのバッジ用） | `count_pending_candidates`, ★`count_unsorted` |
| `GET /api/candidates?status=pending\|rejected` | 候補一覧 | `list_candidates` |
| `POST /api/candidates/{id}/approve` | `{title?, details?, due_date?, due_time?}` で手直しして承認 | `approve_candidate` |
| `POST /api/candidates/{id}/reject` | 却下 | `reject_candidate` |
| `POST /api/candidates/{id}/restore` | 却下を取り消し、pending に戻す | ★`restore_candidate` |
| `GET /api/unsorted` `POST /api/unsorted/{id}/sort` | 書き留めたもの一覧と仕分け（`{kind}`） | ★`fetch_unsorted`, ★`sort_unsorted_item` |
| `GET /api/calendar?month=YYYY-MM` | 日ごとのタスク（タイトル最大 3 件＋件数）・メモ件数・期限切れの有無 | ★`fetch_tasks_due_between`, `fetch_counts_by_date` |
| `GET /api/calendar/{date}` | その日のタスクとメモ | `fetch_tasks_due_on`, `fetch_items_by_date` |
| `GET /api/search?q=&kinds=&include_done=` | 横断検索（⌘K でも使う） | `search_items` |
| `GET /api/ideas` `POST /api/ideas` `GET /api/ideas/{id}` `DELETE /api/ideas/{id}` | セッション。POST は multipart（ファイル）またはテキスト | `doc_loader`, `analyze_source_and_generate_title`, `summarize_and_extract_tasks_from_text`, `generate_initial_mindmap`, `create_idea_session`, `save_mindmap_tree` |
| `POST /api/ideas/{id}/chat` | AI に質問 | `brainstorm_ideas`, `update_idea_session_chat` |
| `POST /api/ideas/nodes/{node_id}/expand` | `{mode: "idea"\|"task"}` で子ノード生成 | `expand_node`, `save_node_expansion` |
| `PATCH /api/ideas/nodes/{node_id}` `DELETE /api/ideas/nodes/{node_id}` | 編集・削除（子孫ごと） | `update_node`, `delete_node_and_descendants` |
| `POST /api/ideas/nodes/{node_id}/to-task` `…/to-memo` | タスク化・メモ化 | `ensure_task_item_for_node`, `ensure_memo_item_for_node` |
| `GET /api/settings` | 下記の状態一式 | `load_config`, `is_connected` ほか |
| `POST /api/settings/google-calendar/connect` `…/disconnect` `…/sync` | Calendar 連携 | `connect`, `disconnect`, `sync_pending_tasks` |
| `POST /api/backup` | バックアップ作成（`backups/` へ） | `create_backup` |

`GET /api/today` の応答:

```json
{
  "date": "2026-10-04",
  "overview": { "today": 3, "overdue": 1, "active": 12, "done_today": 1 },
  "suggestions": [ { "task": { "…": "Task" }, "reason": "期限を1日過ぎています", "due": "2026-10-03" } ],
  "overdue": [ "Task…" ], "today": [ "Task…" ], "later_this_week": [ "Task…" ], "done_today": [ "Task…" ]
}
```

`GET /api/settings` の応答には、`llm_available`（API キーの有無）、`google_calendar: {connected, calendar_id, digest_hour, timezone}`、`mcp: {last_candidate_at}`（`task_candidates` の最新 `created_at`。MCP 経由の受信状況の目安）、`data: {db_path, schema_version}`、`remote: {allowed_hosts}` を含める。

補足仕様:

- **AI を使うエンドポイント**は、API キーが無ければ 503 `{"error":{"code":"llm_unavailable"}}`。LLM 呼び出しのタイムアウトは 60 秒。
- **Google Calendar の接続**は OAuth の画面を Mac のブラウザで開く方式（`InstalledAppFlow`）のため、Mac 上でしか完了できない。リクエストの Host が loopback 以外なら 409 `{"code":"connect_on_host"}` を返し、UI は「Mac で開いて接続してください」と表示する。
- **Calendar の同期**: 変更系のリクエストが成功したら、接続済みのときだけ `api/sync.py` のワーカーに同期要求を積む。ワーカーは 1 本で直列に実行し、要求が溜まっても 1 回にまとめる（`sync_pending_tasks` は冪等なので、launchd の定期同期と重なっても問題ない）。応答は同期を待たない。
- **エラー**: `LookupError` → 404、`ValueError` → 422（メッセージは日本語のまま返す）、二重承認など状態の衝突 → 409。

### 4.5 `core/` への追加・変更（全て pytest を付ける）

1. **マイグレーション v11**: `items.completed_at TEXT`（NULL 可）と `idx_items_completed_at` を追加。
   - `_complete_task_in_transaction` で完了時刻を記録する。
   - `update_item_fields` で done 以外の状態に変えたら `completed_at` を NULL に戻す。
   - 既存の完了済みタスクは NULL のまま（「今日完了」には出ない。これは許容する）。
2. **`reopen_task(item_id, status, discard_next_task_id=None)`**: 完了を取り消す。次の 3 条件を全て満たすときだけ、繰り返しで作った次回分を削除する。
   - `discard_next_task_id` が同じ `recurrence_rule_id` を持つ。
   - 状態が inbox のままである。
   - 作成時刻が元タスクの `completed_at` 以降である。
   削除時は Calendar の outbox にも記録する。
3. **`restore_candidate(id)`**: rejected → pending。approved は 409。
4. **書き留め（未仕分け）**: 「`kind='memo'`・`status='inbox'`・`ai_category IS NULL`」を未仕分けとする（既存の `classify_inbox_items` と同じ判定）。
   - `insert_unsorted(text)` で登録する。
   - `fetch_unsorted()` と `count_unsorted()` で取得・件数確認する。
   - `sort_unsorted_item(id, kind)` で仕分けする。タスクにするときは `kind='task'`、`ai_category='task'`、重要度 3・緊急度 3 を設定し、Calendar の outbox にも積む。
5. **取得関数を追加**: `fetch_item(id)`、`fetch_tasks_completed_on(date)`、`fetch_tasks_due_between(start, end)`、`fetch_task_sources(task_ids)`（`task_candidates.task_id` から出典を引く）。
6. **共通化**:
   - `app.py` の `_task_overview` を `core/overview.task_overview` に移す。
   - `mcp_server._task_title` を `core/titles.task_title` に移す。
   - MCP と Streamlit 版はどちらも移した関数を使うようにする。既存テストはそのまま通ること。
7. **`core/quick_parse.py`**: `parse(text, today) -> ParsedCapture(kind_guess, title, due_date, due_time, recurrence_guess)`。ルールベースでネットワークは使わない。対応する表現は次のとおり。
   - 日付: 今日、明日、明後日、今週◯曜、来週◯曜、◯曜（次に来るその曜日）、`M月D日`、`M/D`、`D日`
   - 時刻: `H時`、`H時半`、`H:MM`、午前・午後、夕方（18:00）、朝（9:00）
   - 期限の言い回し: 「◯◯までに」
   - 繰り返し: 毎日、毎週、隔週、毎週◯曜
   - 種類の判定: 動詞で終わる・期限がある → タスク。「〜かも」「アイデア」「メモ」で始まる → メモ。既存の `classify_text_rule_based` も参考にする。
   - タイトル: 日時を表す部分を取り除いた残り。
   - テストは表形式で 30 例以上。

---

## 5. フロントエンド

### 5.1 ルーティング

| パス | 画面 |
|---|---|
| `/` | `/today` へリダイレクト |
| `/today` | 今日 |
| `/inbox?tab=candidates\|unsorted` | 受信箱 |
| `/all?view=list\|calendar&type=&state=&due=&q=&month=` | すべて（絞り込みは URL に保持し、戻る操作で復元できるように） |
| `/ideas`、`/ideas/:id` | ひらめき |
| `/settings` | 設定 |
| `/login` | ログイン |

**タスク詳細は、どの画面でも `?task=42` で開く。** URL を共有でき、スマホの「戻る」でシートが閉じる。

### 5.2 データと状態

- サーバー状態はすべて TanStack Query で持つ。キーは `["today"]`、`["tasks", filters]`、`["task", id]`、`["inbox-counts"]`、`["candidates", status]`、`["unsorted"]`、`["calendar", month]`、`["ideas"]`、`["idea", id]`、`["settings"]`。
- **楽観的更新**: 完了、元に戻す、状態変更、承認、却下、仕分け。失敗したら元に戻し、トーストで知らせる。
- 変更が成功したら `today`・`tasks`・`inbox-counts`・`calendar` を invalidate する。
- `refetchOnWindowFocus` を有効にする。MCP からの候補追加を、画面に戻ったときに反映するため。
- グローバルな UI 状態は React Context にまとめる。対象は、クイック入力の開閉、トースト、テーマ。
- テーマは `localStorage` に保存する（ライト・ダーク・自動）。起動直後のちらつきを防ぐため、`index.html` のインラインスクリプトで `data-theme` を先に設定する。
- API クライアントは `fetch` の薄いラッパーにする。
  - 状態を変えるメソッドには `X-SymNote: 1` を自動で付ける。
  - 401 が返ったら `/login` に移す。
  - ネットワーク障害のときは「オフラインです」の帯を出す。
- 型は `npm run gen:api` で生成する。このコマンドは FastAPI の `/openapi.json` を取得し、`openapi-typescript` で型にする。CI では、生成結果に差分があれば失敗させる。

### 5.3 レイアウトとブレークポイント（モックの `@media` と同じ）

| 幅 | ナビ | 詳細パネル | クイック入力 |
|---|---|---|---|
| 1181px 以上 | 左サイドバー（248px）。ロゴ、書き留めるボタン、4 項目と件数、設定、保存状態 | 右に 420px の列を追加し、本文を押し縮める | 中央のダイアログ（cmdk） |
| 861〜1180px | アイコンだけのサイドバー（76px） | 右から重なるドロワー | 中央のダイアログ |
| 860px 以下 | 上部のヘッダー（画面名、設定）と下部のタブバー（今日・受信箱・＋・すべて・ひらめき） | 下からのボトムシート（vaul、最大 88vh） | 下からのシート。開くと入力欄に自動でフォーカス |

- スマホでは `env(safe-area-inset-*)` を必ず考慮する。ノッチとホームバーにかからないようにするため。
- タップできる要素の当たり判定は 44px 以上にする。チェックボタンは見た目 22px で、`::after` で当たり判定を広げる（モックの `.check::after`）。
- 本文の最大幅は 760px（`--content-max`）。

### 5.4 デザインシステム

- 色・文字・余白・角丸・動きは、すべて `tokens.css` の変数を通す。16 進の色を直接書かない。ESLint または Stylelint で検出する。
- 書体は 2 つを使い分ける。
  - 明朝（`--font-serif`）: 画面見出し、日付、セクション見出し、おすすめの番号、詳細パネルのタイトルだけに使う。
  - ゴシック（`--font-sans`）: それ以外すべて。数字には `tabular-nums` を付ける。
- 文字色の使い分け:
  - `--ink`: 本文
  - `--ink-2`: 補足
  - `--ink-3`: メタ情報（AA を満たす最も淡い色）
  - `--ink-4`: プレースホルダー、無効、装飾のみ（読ませる文字には使わない）
- 差し色の意味は固定する。
  - 藍（`--accent`）: 主要な操作と選択中のもの
  - 朱（`--danger`）: 期限切れ、重要、削除
  - 抹茶（`--success`）: 完了
  - 黄土（`--ai`）: AI 由来のもの（おすすめ、候補の件数、確度）
- 紙の質感は 2 つの演出で出す。
  - `body::before` のノイズ（ダークでは薄くする）
  - AI カードとマインドマップの方眼ドット
- 動き:
  - ホバー 120ms、チェック 200ms、パネル 320ms。イージングは `--ease-out`。
  - `prefers-reduced-motion` のときはすべて 0ms にする（`tokens.css` で対応済み）。

部品の一覧と、対応するモックのクラス:

| 部品 | モック | 要点 |
|---|---|---|
| `CheckButton` | `.check` | 未完了は丸枠。重要（importance 4 以上）は朱色の枠。完了で抹茶色に塗り、少し拡大する |
| `TaskRow` | `.task` | 行のクリックで詳細を開く。チェックは独立したボタン（`aria-label="完了にする"`）。メタ情報は時刻、繰り返し、出典の点、期限切れ |
| `TaskSection` | `.section` | 明朝の見出しと件数。期限切れは朱色。右端に「追加」や「すべて見る」 |
| `AiPicks` | `.ai-card` `.pick` | 番号は明朝の黄土色。理由を 1 行。「今日に入れる」と「今日に入っています」を切り替える |
| `CandidateCard` | `.cand` | 出典のバッジ、差出人や場所、経過時間、確度のメーター、タイトル、原文の抜粋（引用）、期限チップ、却下・編集して承認・承認 |
| `Segmented` | `.seg` `.mini-seg` | Radix ToggleGroup で作る |
| `PropRow` | `.props` | 詳細パネルの「ラベル＋値」。値はクリックで Popover を開いて編集する |
| `Pips` | `.pips` | 重要度・緊急度の 5 段階。重要度は朱色 |
| `CaptureDialog` | `.capture` | テキスト、推定結果のチップ（種類、日時、繰り返し）、移動と検索の候補、操作のヒント |
| `Toast` | `.toast` | 墨色のピル型。「元に戻す」の操作付き。5 秒で消える |

### 5.5 操作の仕様

**完了**

1. チェックを押すと、200ms でチェックが付く。
2. その行は 1.2 秒その場に残ってから「完了 n件」の折りたたみに移る。押し間違えても慌てずに済むようにするため。
3. トースト「『◯◯』を完了にしました［元に戻す］」を出す。
4. 繰り返しタスクなら、トーストを「次回 10月11日（日）を作りました」にする。
5. 「元に戻す」は `reopen` を呼ぶ。完了前の状態と次回分の ID を渡す。

**状態の名前**: 画面では「あとで・今日・今週・完了」と呼ぶ。`inbox` という語は画面に出さない（「受信箱」と混同するため）。

**クイック入力（⌘K / ＋）**

- 入力のたびに 250ms 待ってから `capture/preview` を呼び、チップを更新する。
- 種類は「タスク・メモ・あとで仕分け」から選ぶ。既定値は推定結果を使う。
- 日時のチップを押すと日付選択が開き、推定を上書きできる。
- 保存は Enter（PC）か「保存」ボタンで行う。改行は Shift+Enter。
- 保存したら入力欄を空にし、ダイアログは開いたままにする。続けて書き留められるようにするため。
- 先頭が `/` のとき、または ⌘Enter を押したときは検索モードにし、`search` の結果と画面移動の候補を出す。

**受信箱**

- 承認すると、カードを右へ退場させる（320ms）。却下は左へ退場させる。
- トーストに「元に戻す」を付ける。却下は `restore` で戻す。承認したタスクの取り消しは、そのタスクの削除と候補の `restore` を順に行う。
- 「編集して承認」は、カードをその場で編集フォームに切り替える。タイトル・期限・時刻を編集できる。
- スマホでは、カードを左右にスワイプして却下・承認できる。閾値は横 96px で、ボタン操作も常に残す。

**キーボード（PC）**

| キー | 動作 |
|---|---|
| ⌘K | クイック入力 |
| `j` / `k` | 行を移動 |
| `x` | 完了 |
| Enter | 詳細を開く |
| `t` | 今日にする |
| `e` | タイトルを編集 |
| Esc | 閉じる |
| `g` → `t` / `i` / `a` / `h` | 今日 / 受信箱 / すべて / ひらめき へ移動 |
| `?` | ショートカット一覧 |

入力欄にフォーカスがあるときは無効にする。

**空の状態**

- 今日にタスクが無いとき: 「今日やることはありません。受信箱に 4 件の候補があります →」
- 受信箱が空のとき: 「確認待ちはありません。Claude に『メールから ToDo を探して』と頼むと、ここに届きます。」

**読み込み中・エラー・AI 不可**

- 読み込み中は、行の形をしたスケルトンを出す。
- エラーは、その場に「再読み込み」ボタンを出す。
- AI を使えないときは、AI のボタンを無効にして「AI キーが未設定です（設定 → AI）」と添える。

### 5.6 画面ごとの仕様

**今日（`/today`）** モック: `pc-today.png`、`sp-today.png`

- 見出しまわり: 年と曜日 → 大きな日付（明朝）→ 一文の要約（今日 n 件、期限切れがあれば朱色で）→ 進み具合のバー（今日完了 / 今日の総数）。
- 今日のおすすめ: `suggestions` を最大 3 件。今日に入っていないものには「今日に入れる」を出す（`PATCH status=today`）。候補が 0 件ならカードごと出さない。
- セクション: 期限切れ → 今日 → 今週のあとで（最大 5 件、「すべて見る」で `/all?due=week`）→ 今日完了（折りたたみ）。

**受信箱（`/inbox`）** モック: `pc-inbox.png`、`sp-inbox.png`

- タブ: 「AI の候補 n」と「書き留めたもの n」。右上に「却下した候補」（status=rejected の一覧。戻せる）。
- 候補カードの確度は、メーターと % で示す。confidence が null のときはメーターを出さない。
- `source_url` があるときだけ、出典の行を外部リンクにする（`rel="noopener noreferrer"`、`target="_blank"`）。
- 書き留めたもの: 本文、時刻、AI の判定（`quick_parse` の推定）、「タスクにする」「メモにする」。

**すべて（`/all`）** モック: `pc-all-detail.png`、`pc-calendar.png`、`sp-calendar.png`

- 表示の切り替え: リスト / カレンダー。右に検索欄（スマホでは 2 行目に全幅で置く）。
- 絞り込みのチップ（スマホでは横スクロール）:
  - 種類: すべて / タスク / メモ
  - 状態: 未完了 / 完了
  - 期限: 期限切れ / 今日 / 今週 / 期限なし
- リストのまとまり: 期限切れ → 今日 → 明日 → 今週 → 来週以降 → 期限なし。メモは作成日のまとまりに入れ、ペンのアイコンで区別する。
- カレンダー:
  - 月曜始まり。今日は藍色の丸、選択した日は薄い藍色で示す。
  - PC はマスにタイトルを最大 2 件と「+n」。スマホは点で示す（期限切れは朱色）。
  - 下に、選択した日のタスクとメモを出す。前月・翌月・今日のボタンを置く。

**タスク詳細（`?task=id`）** モック: `pc-all-detail.png`、`sp-detail.png`

- 上部: タイトルを明朝でその場で編集できる（blur か Enter で保存）。その下にメモ欄（自動で伸びる textarea）。
- 項目の並び: 期限（日付・時刻）→ 繰り返し → 予定（あとで / 今日 / 今週）→ 重要度 → 緊急度 → 所要時間（短い / ふつう / 長い）→ 集中力（低 / 中 / 高）。
- 変更は即時保存する（300ms のデバウンス後に PATCH）。保存ボタンは置かない。
- 出典があれば「Gmail から承認」のカードを出し、原文の URL へのリンクにする。
- 下部: 作成日、ID、削除（確認ダイアログ付き）。繰り返しがあれば「繰り返しを停止」も置く。

**ひらめき（`/ideas`）** モック: `pc-ideas.png`、`sp-ideas.png`

- セッションの一覧と「資料を入れて始める」（ドラッグ＆ドロップ、PDF・DOCX・TXT・MD、またはテキストの貼り付け）。
- 新しく始めると、要約・抽出した ToDo・マップの順に作る。
  - ToDo はチェックを付けて「受信箱に送る」で候補にする（`propose_candidates` に `source="other"`、`source_ref="idea:<セッションID>:<連番>"` で渡す）。承認フローを 1 本にそろえるため。
  - これで旧「ドキュメント分析」を兼ねる。
- マインドマップ:
  - SVG の曲線と DOM のノードで描く（ルートは墨色、ToDo は抹茶色の枠、アイデアは黄土色のラベル）。
  - パン（ドラッグ）とズーム（ピンチ、ホイール）ができる。
  - ノードを選ぶと、下に操作を出す（広げる・ToDo を抽出・タスクにする・メモにする・削除）。
  - 配置ロジック（`app.py` の `_mindmap_nodes_with_positions`）は `core/mindmap.py` に移し、API で座標まで計算して返す。
- 下部の入力欄から AI に質問できる（`chat`）。

**設定（`/settings`）** モック: `pc-settings.png`

- 表示: テーマ（ライト / ダーク / 自動）。
- 連携:
  - Google Calendar（状態、毎朝の通知時刻、接続・解除・今すぐ同期）
  - AI アシスタント（MCP）（最終受信日時、設定方法へのリンク）
- スマホから使う: アクセス先の URL と「QR を表示」。
  - QR には `URL#token=…` を含める。表示前に「この QR を他人に見せないでください」と確認する。
  - トークンの再発行ボタンを置く。再発行すると、すべての端末がログアウトする。
- データ: 保存場所（DB のパス）、スキーマ版数、「バックアップを作成」。

### 5.7 PWA とスマホからの接続

- `vite-plugin-pwa` の manifest:
  - `name: "SymNote"`、`display: "standalone"`
  - `theme_color`・`background_color` は `#f5f1e8`（ダークは `media` 付きの `<meta name="theme-color">` で `#1c1a17`）
  - アイコンは 192、512、maskable、apple-touch-icon
- サービスワーカー:
  - アプリの外枠（JS、CSS、フォント）だけを事前キャッシュする。
  - `/api/*` は常にネットワークに取りに行く。v1 ではオフライン中の書き込みはしない。オフラインの帯を出して操作を無効にする。
- iOS でサービスワーカーとホーム画面追加を使うには HTTPS が要るため、Tailscale を使う。
  1. Mac とスマホに Tailscale を入れ、同じアカウントでログインする。
  2. Mac で `tailscale serve --bg 8765` を実行する。`https://<mac名>.<tailnet>.ts.net` で公開され、外部のインターネットには出ない。
  3. スマホで設定画面の QR を読む。ログインしたら「ホーム画面に追加」する。
- `docs/launchd/com.symnote.web.plist.example` を用意する（`KeepAlive`、ログは `/tmp/symnote-web.log`）。README に、上の手順と一緒に書く。

---

## 6. フェーズと PR 分割（Codex への依頼単位）

各 PR は単独でマージでき、マージ後も Streamlit 版・MCP・calendar_sync が動くこと。
ブランチは `codex/redesign-<番号>-<名前>` とする。

| PR | 内容 | 完了条件 |
|---|---|---|
| **フェーズ 0: core の準備** | | |
| PR0 | §4.5 のすべて（v11 マイグレーション、`reopen_task`、`restore_candidate`、未仕分け、取得関数、共通化、`quick_parse`） | 既存 94 件と追加テストがすべて通る。v10 の DB を v11 にしてもデータが失われない。`quick_parse` の表形式テストが 30 例以上 |
| **フェーズ 1: API** | | |
| PR1 | API の基盤（`create_app`、`server`、認証、CSRF、TrustedHost、エラー変換、静的配信、`health`、`symnote-web` コマンド、`web` の extras） | 認証なし・ヘッダーなし・不正な Host がすべて拒否されるテスト。トークンファイルの権限が 600 |
| PR2 | 今日・タスク・メモ・検索・クイック入力の API | 全エンドポイントで正常系・404・422 のテスト。完了 → 元に戻す で繰り返しの次回分が消えること |
| PR3 | 受信箱（候補・未仕分け）・カレンダーの API。変更後の Calendar 同期ワーカー | 二重承認が 409。同期が直列に 1 本で動くこと（モックで検証） |
| PR4 | 設定・Google Calendar・バックアップ・ひらめきの API（LLM は依存性で差し替えられるようにする） | LLM キーなしで 503。偽の LLM でひらめきの一連の流れが通る |
| **フェーズ 2: 画面の骨格** | | |
| PR5 | `web/` の初期設定、トークンとフォント、AppShell（サイドバー・タブバー・ヘッダー）、ルーティング、ログイン、API クライアントと型生成、テーマ、トースト、FastAPI からの配信 | 3 つの幅でナビがモックと一致する。ログインからナビの移動までの E2E |
| **フェーズ 3: 毎日使う画面** | | |
| PR6 | 今日の画面とタスク詳細（パネルとシート） | §5.5「完了」の挙動。詳細で編集すると即時保存される。`sp-today` と `pc-all-detail` に見た目が近い |
| PR7 | クイック入力（⌘K と ＋、推定チップ、検索モード） | 「明日の18時までに◯◯」が、期限付きのタスクとして保存される |
| **フェーズ 4: 整理する画面** | | |
| PR8 | 受信箱（候補と書き留めたもの、スワイプ、却下一覧） | 承認と却下、元に戻す。XSS 文字列が文字として表示される |
| PR9 | すべて（リスト、絞り込み、検索、カレンダー） | 絞り込みが URL に残り、戻る操作で復元される。月の移動 |
| **フェーズ 5: ひらめき** | | |
| PR10 | セッション一覧、資料の取り込み、要約と ToDo（受信箱へ送る） | 偽の LLM で E2E が通る |
| PR11 | マインドマップのキャンバス（パン・ズーム・選択・展開・タスク化）と AI チャット | スマホでピンチとドラッグができる |
| **フェーズ 6: 仕上げ** | | |
| PR12 | 設定の画面、PWA、QR ログイン、launchd の例、README | iPhone の Safari でホーム画面に追加でき、起動できる（手動確認の手順を PR に書く） |
| PR13 | E2E 一式（§7.3）、アクセシビリティ（axe）、キーボード操作、性能の確認 | Playwright がデスクトップとモバイルの両方で通る。axe の重大な違反が 0 件 |
| PR14 | 切り替え: README の起動方法を `symnote-web` に変える。Streamlit 版は `symnote-legacy` として残す | ユーザーが 1〜2 週間使ったあと、別の PR で Streamlit 版を削除する（**ユーザーの了承が必要**） |

依存関係: PR0 → PR1 → (PR2, PR3, PR4) → PR5 → PR6 → PR7 → (PR8, PR9) → (PR10 → PR11) → PR12 → PR13 → PR14。
括弧内の PR は並行して進めてよい。

---

## 7. テスト方針

### 7.1 Python（pytest）

- `core` で追加するものすべての単体テスト。対象はマイグレーション v10→v11、`reopen_task` の 3 条件、`quick_parse` の表形式テスト、未仕分けの仕分け。
- API のテストは FastAPI の `TestClient` と一時 DB を使う。各エンドポイントで次を確かめる。
  - 正常系
  - 認証なしで 401
  - `X-SymNote` なしで 403
  - 不正な Host で 400
  - 存在しない ID で 404
  - 入力が不正なとき 422
- LLM と Google の API は依存性を差し替えて偽物にする。テストからネットワークに出ない。
- 既存のテスト（`test_mcp_server` など）はすべてそのまま通ること。

### 7.2 フロントエンド（Vitest）

- `lib/dates.ts`: 「昨日まで」「10月4日（日）」「来週」の表記。
- 楽観的更新のフック: 失敗したら元の状態に戻ること。
- `CaptureDialog`: 推定チップの表示と上書き。

### 7.3 E2E（Playwright）

- プロジェクトは 2 つ。デスクトップ（1440×900）とモバイル（iPhone 13、390×844、タッチ）。
- 起動手順: 一時 DB を作る → `symnote-web` を起動する（偽の LLM を使う設定にする）→ シードデータを入れる → 実行する。
- シナリオ:
  1. ログインする。
  2. 今日の画面でタスクを完了し、元に戻す。
  3. 繰り返しタスクを完了し、次回分が作られる。
  4. クイック入力でタスクとメモを作る。
  5. 候補を承認・却下し、元に戻す。
  6. タスクの詳細を編集する。
  7. 絞り込みと検索をする。
  8. カレンダーで月を移動する。
  9. ひらめきで資料を取り込み、ToDo を受信箱へ送る。
  10. テーマを切り替える。
  11. `<img src=x onerror=alert(1)>` を含む候補が文字として表示される。
  12. API キーがないとき、AI のボタンが無効になっている。
- 見た目の確認: 主要な画面のスクリーンショットを `web/tests/e2e/__screenshots__` に保存する。`docs/redesign/mockups/screens` と並べて PR に貼る（ピクセル一致を合格条件にはしない）。

---

## 8. Codex への依頼文のひな形

```
目的: docs/redesign/PLAN.md の PR<番号>「<内容>」を実装する。
前提: AGENTS.md と PLAN.md §2「守ること」を必ず守る。実 DB と .env には触れない。
参照: PLAN.md §<該当節>、モック docs/redesign/mockups/index.html（<クラス名>）、
      スクリーンショット docs/redesign/mockups/screens/<ファイル>.png
範囲: <触ってよいファイルとディレクトリ>。範囲外の変更が必要なら、理由を書いて最小限にする。
完了条件: PLAN.md §6 の PR<番号> の完了条件。加えて `python -m pytest` がすべて通ること（PR5 以降は `cd web && npm run lint && npm test && npm run test:e2e` も）。
報告: 変更したファイル、テスト結果（件数）、モックと意図的に変えた点、未解決のリスク。
```

---

## 9. リスクと未決事項

| 項目 | 内容 | 対応 |
|---|---|---|
| 作業量 | マインドマップの移植が最も重い | PR11 に分けた。遅れたら旧コンポーネントを iframe で一時的に埋め込む案に切り替える |
| Calendar の接続 | OAuth は Mac 上でしか完了できない | スマホからは「Mac で接続してください」と表示する（§4.4） |
| 並行稼働 | Streamlit 版・Web 版・MCP・launchd が同じ DB に書き込む | WAL と、短いトランザクションの既存方針で対応する。同期は冪等 |
| LLM の待ち時間 | ひらめきの生成は 10 秒以上かかることがある | 進捗の表示とキャンセル。タイムアウトは 60 秒 |
| フォント | 明朝とゴシックで数 MB になる | 日本語のサブセット版（@fontsource の unicode-range 分割）を使い、必要な分だけ読み込む |
| タイトルの保存先 | 今は `items.tags` をタイトルとして使っている | v1 は互換のためこのままにする。`title` 列を独立させるのは別の計画にする |
| ビルドの手間 | Node が必要になる | README に `npm ci && npm run build` を明記する。`web/dist` はコミットしない |
| 既存の完了済みタスク | `completed_at` が無いため「今日完了」に出ない | 許容する（v11 以降の完了から記録される） |
