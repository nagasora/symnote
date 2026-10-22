# SymNote ゲーム感覚リデザイン 実装指示書（Codex 向け）

- 作成: 2026-10-22（依頼者: sora）
- 実装担当: Codex（本書の PR 単位で順に実装する）
- 対象: Web 版（`web/` の React 19 + Vite、`src/symnote/api/` の Starlette API、`src/symnote/core/`）
- 前提文書: `AGENTS.md`（必読・最優先）、`docs/redesign/PLAN.md`（API と画面の土台。見た目と言葉は本書が上書きする）
- **`docs/redesign/CODEX_GAME_UI_BRIEF.md`（同日朝の版）は本書で置きかえる。** あちらで「Codex が決めてよい」としていた世界観・配色・絵柄は、本書 §2 で決定済み。食い違う箇所は本書に従う
- 対象コード: sora の Mac の `~/work/apps/symnote`（2026-10-22 15:00 時点で確認。SQLite／PostgreSQL 両対応、iOS（Capacitor）版あり）

---

## 0. この文書の使い方

| 資料 | 場所 | 用途 |
|---|---|---|
| 指示書（本書） | `docs/redesign/game/CODEX_GAME_REDESIGN.md` | 何を・どの順で・どう作るか。**モックと食い違ったら本書を優先** |
| デザイントークン | `docs/redesign/game/tokens.game.css` | 色・文字・角丸・動き。`web/src/styles/tokens.css` をこれで置き換える |
| 画面モック（ソース） | `docs/redesign/game/mockups/*.dc.html` | 見た目の正解。インライン style と `<helmet><style>` のクラスがそのまま部品の仕様。`support.js` は無いのでブラウザでは崩れる。**ソースを読んで値を移す** |

モック一覧:

| ファイル | 画面 |
|---|---|
| `Concept.dc.html` | 原則・ことばの置きかえ・色・文字・部品（クエスト行、ボタン、XP バー、相棒の表情） |
| `PcToday.dc.html` | PC きょうの冒険（3 カラム） |
| `PcMap.dc.html` | PC 冒険マップ＋たね |
| `SpToday.dc.html` | スマホ きょう |
| `SpClear.dc.html` | スマホ クエストクリア演出 |
| `SpCapture.dc.html` | スマホ ＋ミナに渡す（入力シート） |
| `SpInbox.dc.html` | スマホ 依頼ボード |
| `SpSeed.dc.html` | スマホ たね → 地図（アイデアをゴールに分解） |
| `SpMap.dc.html` | スマホ 冒険マップ |
| `SpMe.dc.html` | スマホ じぶん（レベル・あしあと・実績・ショップ） |

---

## 1. なぜ作り直すか（sora の声）

> 今のデザインは AI 感があって、温かみがなくて、毎日使いたいと思えない。「ゲーム感覚で毎日の Todo をこなせる」感じにしたい。毎日の Todo 管理、タスクの生成、アイデアを形にする構造を、スマホとパソコンの両方で使いやすく。

現状の問題（コードで確認済み）:

1. **AI が前面に出すぎ。** 「✦ いま着手する1件（AIおすすめ）」「✦ 頭の中をまるごと書き出す（AIが自動でタスク分解）」など、✦ と「AI」の文字が主役になっている（`web/src/App.tsx` 今日画面）。
2. **ゲームの仕組みが Web 版に出ていない。** `core/quest.py` に XP・ゴールド・レベル・称号・実績・ご褒美交換が実装済みで、`db.complete_task` の中で報酬も付与されているのに、API にも React にも出口がない。完了しても何も起きない。
3. **見た目が冷たい。** 明朝見出し＋生成りの紙＋藍の差し色は「上品な道具」で、触って楽しい手応えがない。
4. **アイデア → ゴール → きょうの一歩 の道筋が見えない。** ゴール（`goals` テーブルの木構造）とひらめき（マインドマップ）が別々で、つながりが画面にない。
5. **`App.tsx` がまだ 1,740 行。** 受信箱・ひらめき・設定は `web/src/screens/`、ゴールは `GoalsView.tsx` に出ているが、今日・すべて・状態管理・API 呼び出しが `App.tsx` に残っている。画面を作り直す前に分割を終える。

---

## 2. デザインの方針「ひだまりの冒険」

### 2.1 三つの原則（すべての判断はここに戻る）

1. **AI は相棒の声で話す。** 「AI」「✦」を UI から消す。提案・分解・要約はすべて相棒「ミナ」（`secretary.load_settings().name`。既定ミナ、設定で変更可）の吹き出しで、一人称のやさしい短文（2 行まで）。登録・完了・延期は必ず人が押してから反映する（既存方針どおり）。
2. **責めない。** 期限切れは赤字にせず「持ち越し」と呼ぶ。減点しない。連続記録は週 1 日まで空白を自動で補う（おやすみ券）。数日ぶりに開いたら「おかえり」から始める。
3. **押した手応え。** ボタンとカードに「厚み」（下方向の影）があり、押すと沈む。チェックは弾み、+XP が浮かび、バーが伸びる。きょうの分を全部クリアしたら宝箱。

### 2.2 ことばの置きかえ（UI 文言はすべてこれに従う。DB・API の名前は変えない）

| いま | 新しい名前 | 備考 |
|---|---|---|
| 今日 | きょうの冒険（タブ名は「きょう」） | |
| タスク / 完了 | クエスト / クリア | 内部名は task のまま |
| 期限切れ | 持ち越し | 朱色・赤字を使わない |
| 大きいタスク | 大物クエスト | 星 3 のもの |
| 受信箱・AI 候補 | 依頼ボード（タブ名は「依頼」） | 候補＝依頼、未仕分けメモ＝メモ、AI 進捗報告＝AI の報告 |
| ゴール / 子ゴール | 冒険マップ / ステージ | `goals.parent_id` の木をそのまま使う |
| アイデア / ひらめき | たね | |
| 秘書 | 相棒（ミナ） | |
| 書き留める（⌘K） | ミナに渡す（⌘K / ＋） | |
| AI おすすめ | つぎの一手 | |
| 承認 / 却下 | 受ける / 見送る | |

### 2.3 見た目

- **トークン**は `tokens.game.css` のみを使う。色の意味は固定: おひさま＝主ボタン・ゴールド・つぎの一手、わかば＝クリア・XP、そら＝予定・マップ進捗、いちご＝大物・連続記録、ぶどう＝たね・マップ名。
- **色付きの面の上の文字はココア（`--ink`）。白文字は使わない**（黄・緑の上の白はコントラスト不足）。
- **書体**: 見出し・数字 `--font-display`（800）、本文 `--font-body`（500/700）。数字は `font-variant-numeric: tabular-nums`。`@fontsource/m-plus-rounded-1c` と `@fontsource/zen-maru-gothic` を同梱し、外部フォントは読まない。
- **形**: カード角丸 20px・枠 2px `--line`・影 `--lift-card`。ボタン角丸 16px、高さ 48px（小 44px）、主ボタンは `--sun` ＋ `--lift-sun`。押下で `translateY(var(--press))` と影 0。
- **アイコン**: 線幅 2.2 の丸い線アイコン（モックのインライン SVG と同じ形。`lucide-react` で同等のものがあれば使ってよい）。**絵文字を UI に使わない。** `core/quest.py` の実績アイコン（絵文字）は、フロント側で `code → SVG` に対応表で置きかえる。
- **相棒ミナ**: `web/src/components/buddy/Buddy.tsx` に SVG で実装（モックの SVG をそのまま移植）。`expression: "normal" | "happy" | "sleepy"`、`size`。体色は `--buddy-body`。
- **区切り**: リスト行の区切りは 2px の破線（`border-top: 2px dashed var(--line)`）。
- ライト・ダーク両対応（ダークは「夜のキャンプ」）。

---

## 3. 情報設計

### 3.1 画面の構成（4 タブ ＋ ミナに渡す）

| タブ | 中身 | 旧画面 |
|---|---|---|
| **きょう** `/today` | 相棒のひとこと／つぎの一手／きょうの進み具合と宝箱／きょうのクエスト（体力メーター）／持ち越し／クリア済み（折りたたみ）／PC 右列にプレイヤー状態・きょうの予定・進めている冒険 | 今日 |
| **マップ** `/map` | 冒険マップ一覧と道（ステージとクエスト）／たね（アイデア）／切替で「ぜんぶ」（全クエスト一覧・検索）と「予定表」（月カレンダー） | すべて、ひらめき、ゴール |
| **依頼** `/board?tab=requests\|memos\|reports` | 依頼（メール・Slack・MCP 由来の候補）／メモ（未仕分け）／AI の報告（委任タスクの進捗報告） | 受信箱 |
| **じぶん** `/me` | レベル・XP・連続・ゴールド／冒険のあしあと（4 週間）／実績／ごほうびショップ／冒険日誌（週次ふりかえり）／設定への入口 | 設定、クエスト |
| **＋ ミナに渡す** | どこからでも開く入力シート（PC は中央ダイアログ＋⌘K、スマホは下からのシート） | クイック入力、ブレインダンプ |

設定は `/me/settings`（表示・相棒・連携・データ・招待）。タスク詳細はどの画面でも `?quest=ID` で開く（PC 右ドロワー、スマホ下シート）。

### 3.2 レイアウトとブレークポイント

| 幅 | ナビ | 右列 | 詳細 | ミナに渡す |
|---|---|---|---|---|
| 1280px 以上 | 左サイドバー 240px（ロゴ、ミナに渡すボタン、4 タブ、下部にミニ XP バー） | 340px（プレイヤー状態・きょうの予定・進めている冒険） | 右からドロワー | 中央ダイアログ |
| 861〜1279px | アイコンだけのサイドバー 76px | なし（中身は本文の末尾へ） | 右からドロワー | 中央ダイアログ |
| 860px 以下 | 上部ヘッダー（画面名・連続・ゴールド）＋下部タブバー（きょう・マップ・＋・依頼・じぶん。＋は中央で浮いた 60px のおひさまボタン） | なし | 下からシート（最大 88vh） | 下からシート |

スマホは `env(safe-area-inset-*)` を必ず考慮。当たり判定は 44px 以上。

### 3.3 毎日の流れ（ゲームループ）

```
朝  開く → ミナ「おはよう。きょうは3つで十分」→ つぎの一手が 1 枚
       （きょうのクエストが 0 件なら「作戦会議」カード：ミナが 3 件提案 → 選んで「きょうに入れる」）
日中 はじめる（フォーカスタイマー）→ クリア → +XP / +G 演出 → つぎの一手が繰り上がる
       依頼ボードに届いた依頼を 受ける / 見送る（+2 XP、1 日 20 件まで）
       思いついたら ＋ ミナに渡す（1 行ならそのまま、複数行ならミナが分ける）
全部クリア → 宝箱（デイリークリアボーナス）
夜  18 時以降に開くと「きょうのふりかえり」カード：クリア数・獲得 XP・あしたの最初の一歩
```

### 3.4 クエストの見せ方

| 項目 | 規則 |
|---|---|
| 星（重さ） | `estimate_minutes` があれば ≤15 分 ★、≤60 分 ★★、それ以上 ★★★。無ければ `effort` short/medium/long を ★/★★/★★★。どちらも無ければ ★ |
| 大物 | 星 3 のもの。チェック枠を `--berry`、チップ「大物 ★★★」、行に「小分けにする」ボタン（既存 `POST /api/tasks/{id}/breakdown`） |
| 報酬の予告 | 行の右端に `+N XP`（`core/quest._calculate_reward` と同じ式で、いま完了した場合の値） |
| 予定・期限 | `--sky` のチップ（「18:00 まで」「金曜まで」） |
| マップ名 | `--grape` のチップ（`goal_title`） |
| 出典 | 淡いチップ（「Gmail から」） |
| 持ち越し | `--surface-2` の地に「きのうまでの予定でした」。操作は「きょうやる」「日を変える」（スマホは左スワイプでも日を変える） |
| 体力メーター | きょうのクエストの見積もり合計 / カレンダーの空き時間。空きを超えたらミナが「ちょっと多いかも。1つ あしたにする？」 |

### 3.5 クリアの演出（`SpClear.dc.html`）

1. チェックを押す → 240ms でチェックが弾む（`--ease-pop`）、行の上に `+N XP` が浮いて消える、ヘッダーの XP バーと進み具合バーが伸びる。ここまでは画面遷移なし。
2. 次のどれかに当たるときだけ全面のクリア画面を出す: レベルアップ、実績解除、宝箱（デイリークリア）、大物クエストのクリア。それ以外は 1 のみ（毎回全面だとうるさい）。
3. 全面画面: 大きなチェック、「クエストクリア！」、+XP / +G、ボーナスのチップ、Lv バー、宝箱までの残り、ミナのひとこと（よろこび顔）、「つぎのクエストへ」「ひと休み」「元に戻す」。
4. 「元に戻す」は既存の reopen。**報酬は返還しない**（`award_for_completion_in_transaction` の既存仕様。再完了しても二重付与されない）。
5. 音（任意・既定オフ、`secretary.sound_enabled`）と振動（`navigator.vibrate(12)`、対応端末のみ）。`prefers-reduced-motion` のときは紙吹雪・弾みを止め、数値の表示だけにする。

### 3.6 タスクを生み出す入口（3 つに整理）

1. **ミナに渡す（＋ / ⌘K）**: 1 つの入力欄。
   - 1 行 → 既存 `POST /api/capture/preview` のルール解析で日時・種類をチップ表示 → Enter でそのまま登録。
   - 2 行以上 → 既存 `POST /api/capture/split` で分割プレビュー（クエスト / たね / メモに自動判定、行ごとにチェックで外せる、マップの割り当て候補を表示）→「N つ登録する」で既存 `POST /api/capture/commit-split`。「まとめて1件」も残す。
   - 下段に入口ボタン: 写真・資料（新規 `POST /api/capture/file`。§4.5）、声で（Web Speech API があれば。無ければ非表示）、議事録・メール（貼り付けて分割と同じ流れ）。
2. **依頼ボード**: Gmail / Slack / MCP から来た候補（既存 candidates）。カード: 出典バッジ・差出人・経過時間・タイトル・原文の引用・期限チップ・マップ割り当て候補・「見送る」「直して受ける」「受ける」。
3. **マップから**: ステージの「次の一歩をきょうに入れる」「次のステージの地図を描く」。

### 3.7 アイデアを形にする（たね → 地図 → きょうの一歩）

```
たね（メモ） ──育てる──▶ ミナの 3 つの質問 ──▶ 地図の案 ──この地図で冒険をはじめる──▶ 冒険マップ
                         なぜ？                ステージ 3〜5 個                        goal（親）
                         できたらどうなる？    ステージ 1 だけクエスト 2〜4 個          ├ stage goal × N（子）
                         いつまで？                                                   └ ステージ1 のクエスト（items, goal_id=stage）
                                                                                       最初の 1 件は「きょう」に入れるか聞く
```

- **段階的に描く（ローリング計画）**: 最初はステージ 1 のクエストだけ作る。ステージ 1 がすべてクリアされたら、ステージ 2 に「次のステージの地図を描く」ボタンが出る（既存 `POST /api/goals/{id}/breakdown` で提案 → 確認 → 既存 `POST /api/goals/{id}/breakdown/commit` で作成）。先の細かいタスクを最初に大量生成しない。
- **質問は固定の 3 つ**（AI 不要）。回答は `goals.why` / `goals.success_criteria` / `goals.target_date` にそのまま入る。
- **AI が使えないとき**は、ルールで 3 ステージ（準備する／進める／仕上げる）と、既存 `goal_breakdown_handler` のフォールバックと同じ 2 件のクエストを出す。
- マインドマップ（既存のひらめきセッション）は、たね詳細の「ミナとひろげる」から開く上級者向けの道として残す。

### 3.8 冒険マップの見せ方

- マップ＝トップレベルの goal（`parent_id IS NULL`）。ステージ＝その子 goal（`order_index` 順）。ノード＝ステージに属する未完了・完了タスク。
- 道: ノードを縦に並べ、左右にずらして点線でつなぐ（PC は直線＋左右オフセット、スマホは SVG の曲線）。ノードの状態は クリア（わかば・チェック）／いまここ（おひさま・星・光の輪）／これから（灰・ロック）／ゴール（旗）。
- 「いまここ」＝最初の未完了ステージの最初の未完了タスク。きょうに入っていなければ「きょうに入れる」。
- 放置: `goals.stale_goals()` の結果を「◯日 ひと休み中」（`--sun-ink`）で表示し、ミナが「5分でできる一歩を出してみる？」と提案（既存 breakdown を 1 件・5 分指定で呼ぶ）。

---

## 4. バックエンドの追加（すべて pytest を付ける）

### 4.1 守ること

- `AGENTS.md` のとおり。実 DB・`.env` に触らない。テストは一時 DB。
- **マイグレーションは追加のみ。** SQLite の `core/db.py` `SCHEMA_VERSION` 17 → 18、PostgreSQL の `core/postgres.py` `POSTGRES_SCHEMA_VERSION` 17 → 18 を**両方**上げる。PostgreSQL 側は `core/postgres_schema.sql` に同じテーブルを足し、`_IDENTITY_TABLES` に `quest_bonus_log` を加える。v17 の DB（両方）で起動して壊れないテストを付ける。
- 削除は既存のゴミ箱（`/api/trash`）の流れを使い、新しい削除経路を作らない。
- iOS 版（`NativeShell.tsx` / `NativeCapture.tsx` / Capacitor）を壊さない。トークンとフォントは iOS 版にも効くので、`npm run ios:build:simulator` が通ることを確認する。
- **MCP サーバー・`calendar_sync`・Streamlit 版の挙動を変えない**（既存テストが全部通ること）。報酬計算を変えるときは Streamlit 版の表示も同じ関数を使うので、変化はテストで固定する。
- 外部由来の文字列は常に文字列として描画（`dangerouslySetInnerHTML` 禁止）。

### 4.2 スキーマ v18（SQLite と PostgreSQL の両方）

```sql
-- 宝箱・仕分けなど、クエスト完了以外のボーナス。kind+ref で一意にして二重付与を防ぐ
CREATE TABLE IF NOT EXISTS quest_bonus_log (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('daily_clear', 'triage')),
  ref TEXT NOT NULL,               -- daily_clear: 'YYYY-MM-DD' / triage: 'candidate:ID' or 'unsorted:ID' or 'report:ID'
  xp INTEGER NOT NULL CHECK(xp >= 0),
  gold INTEGER NOT NULL CHECK(gold >= 0),
  created_at TEXT NOT NULL,
  seen INTEGER NOT NULL DEFAULT 0,
  UNIQUE(kind, ref)
);
CREATE INDEX IF NOT EXISTS idx_quest_bonus_created ON quest_bonus_log(created_at);
```

### 4.3 `core/quest.py` の追加・変更

1. `_stats` と `_gold_balance` に `quest_bonus_log` の xp / gold を加える（`focus_sessions` と同じ扱い）。
2. **おやすみ券（自動）**: `_streak` を、同じ ISO 週の中で 1 日だけの空白は連続とみなすように変える。2 日続く空白、または同じ週の 2 回目の空白で途切れる。今日まだ未クリアでも昨日から続く既存仕様は維持。戻り値に `rest_days_used_this_week` を足した `StreakInfo` を返す関数 `streak_info(today)` を新設し、`PlayerStats.streak_days` は従来どおり int。表形式のテスト 10 例以上。
3. `quest_preview(task: Mapping, now: datetime) -> QuestPreview(xp, gold, stars, is_boss)`。`_calculate_reward` を書き込みなしで呼ぶ。星は §3.4 の規則。
4. `reward_for_task(task_id) -> Optional[QuestReward]`: `quest_log` からその task の報酬を読む（`seen` は変えない）。`mark_reward_seen(task_id)`。
5. `award_daily_clear_if_eligible(today) -> Optional[BonusReward]`: きょうの対象（期限が今日以前の未完了＋状態 today の未完了）が 0 件、かつ今日クリアが 1 件以上のとき、1 日 1 回だけ XP 20 / G 10 を付与。`complete_task` の後に API から呼ぶ。
6. `award_triage(ref) -> Optional[BonusReward]`: XP 2 / G 0。同じ日の triage が 20 件を超えたら 0（付与しない）。
7. `QuestReward.title` を `_quest_title(task)` に揃える（今は `raw_text` 全文が入っている）。

### 4.4 `core/goals.py` の追加

1. `propose_seed_plan(title, why, done_when, target_date, *, today) -> SeedPlan(stages: [Stage(title, why, target_date)], first_quests: [ProposedTask])`。ステージ 3〜5、最初のクエスト 2〜4（各 `estimate_minutes` 5〜60）。LLM 呼び出しと検証は `propose_goal_breakdown` / `_validate_breakdown` と同じ作法。失敗・キー無しはルールのフォールバックを返し `ai_used=False`。
2. `adopt_seed_plan(seed_item_id, plan, *, today_first: bool) -> {goal_id, stage_ids, task_ids}`: 既存の `goals.create_goal` と `goals.apply_goal_breakdown` を使い、1 トランザクションで 親 goal（why / success_criteria / target_date）→ 子 goal（ステージ、order_index）→ ステージ 1 のタスク（`goal_id` = ステージ 1）を作る。`today_first` なら最初のタスクを status `today` に。たねのメモの `items.goal_id` に親 goal を入れて「地図になった」印にする（メモ本体は消さない）。Calendar の outbox はタスク作成の既存関数を通して積む。
3. `list_map(goal_id) -> MapView`: §3.8 の道を組み立てる（ステージ、各ステージのタスク、状態、いまここ）。

たねの定義: `kind='memo'` かつ `ai_category IN ('idea','someday')` かつ `goal_id IS NULL`、または ユーザーが「たねにする」を選んだメモ（`ai_category='idea'` に更新）。`fetch_seeds()` を `core/db.py` に追加。

### 4.5 API（`src/symnote/api/app.py` に追加。すべて `_with_auth`）

| メソッド・パス | 内容 |
|---|---|
| `GET /api/player` | `{level, title, xp_total, xp_into_level, xp_for_next, gold, streak_days, rest_days_used_this_week, completed_today, buddy: {name, tone, sound_enabled}}` |
| `GET /api/today`（拡張） | 既存に追加: `player`（上と同じ）、`capacity: {planned_minutes, free_minutes|null}`、`daily_clear: {target, done, claimed}`、`period: "morning"|"day"|"evening"`、`buddy_line: string`（下記）。各タスクに `quest: {xp, gold, stars, is_boss}` |
| `GET /api/tasks`・`GET /api/tasks/{id}`（拡張） | 各タスクに `quest` を付ける（`present_task` に引数で渡す。未完了のみ） |
| `POST /api/tasks/{id}/complete`（拡張） | 既存の応答に `reward: {xp, gold, bonuses[], level_before, level_after, new_achievements[{code,title}]}|null`、`daily_clear: {xp, gold}|null`、`player` を足す。報酬を返したら `mark_reward_seen` |
| `POST /api/candidates/{id}/approve`・`/reject`、`POST /api/unsorted/{id}/sort`（拡張） | 応答に `triage_bonus: {xp}|null` |
| `GET /api/reports` `POST /api/reports/{id}/approve` `POST /api/reports/{id}/reject`（**新規**。Web 版に未実装） | AI の報告タブ。`delegation.list_pending_reports` / `approve_report` / `reject_report` を呼ぶ。承認・却下の応答にも `triage_bonus` |
| `POST /api/capture/file`（**新規**・任意） | multipart で PDF / 画像 / テキストを受け、`doc_loader` → `task_extraction` で分割プレビューと同じ形を返す。サイズ上限 10MB、拡張子の許可リスト。未実装の間は「写真・資料」ボタンを出さない |
| `GET /api/achievements` | 全実績（未解除含む） |
| `GET /api/rewards` `POST /api/rewards` `POST /api/rewards/{id}/redeem` `DELETE /api/rewards/{id}`（`active=0`） | ごほうびショップ。不足は 409 `{"code":"insufficient_gold"}` |
| `GET /api/quests/history?days=28` | 日別のクリア数（あしあと用）と、おやすみ券で補った日 |
| `GET /api/seeds` `POST /api/seeds`（テキストからたね作成） `POST /api/memos/{id}/to-seed` | たね |
| `POST /api/seeds/{id}/plan` | body `{why, done_when, target_date|null}` → `SeedPlan`＋`ai_used` |
| `POST /api/seeds/{id}/adopt` | body `{plan（編集後）, today_first}` → 作成結果 |
| `GET /api/maps` | トップレベル goal の一覧（進捗%、現在ステージ番号/総数、`stale_days`） |
| `GET /api/maps/{goal_id}` | `list_map` の結果 |
| `GET /api/review/weekly` | `weekly_review.generate_weekly_review()` を返す（冒険日誌） |

`buddy_line`（相棒のひとこと）: **ルールで作る**（ネット不要・即時）。優先順: 久しぶり（最終クリアから 3 日以上）→「おかえり。まずは1つだけ、いっしょにやろう」／持ち越しあり →「持ち越しが◯つ。小さくしてもいいよ」／体力オーバー →「ちょっと多いかも。1つ あしたにする？」／朝 →「おはよう、{user_name}。きょうは◯つで十分だよ。まずは『{つぎの一手}』から」／夜 →「おつかれさま。きょうは◯つクリア、+◯XP」。呼び名・名前は `secretary.load_settings()`。LLM 版の朝のまとめは既存の秘書機能のまま（任意で後から差し替え可能な関数にしておく）。

---

## 5. フロントエンド

### 5.1 構成（`App.tsx` を分割する）

```
web/src/
  main.tsx  App.tsx（ルーターとシェルだけ）  routes.tsx
  styles/   tokens.css（tokens.game.css をコピー） base.css  motion.css
  api/      client.ts（今の api.ts を移す）  types.ts  hooks.ts（useToday, usePlayer, ...）
  components/
    ui/       Button Card Chip Segmented IconButton Sheet Dialog Toast ProgressBar Kbd EmptyState
    buddy/    Buddy.tsx  BuddyBubble.tsx
    quest/    QuestRow QuestList QuestSection CheckButton QuestChips XpFloat NextActionCard
    game/     PlayerCard XpBar StreakChip GoldChip ChestProgress ClearCelebration AchievementBadge
    layout/   AppShell Sidebar TabBar MobileHeader RightRail DetailDrawer
    capture/  CaptureSheet ParsedChips SplitPreview
  features/
    today/  map/（MapList, MapPath, SeedShelf, SeedGrow, AllQuests, CalendarView）
    board/  me/（MePage, Footprints, Achievements, RewardShop, Journal）  settings/  auth/
  lib/      dates.ts  keyboard.ts  theme.ts  useMediaQuery.ts  swipe.ts  sound.ts  haptics.ts
```

- 追加依存は最小限: `react-router-dom`（URL と戻る操作）、`@fontsource/m-plus-rounded-1c`、`@fontsource/zen-maru-gothic`、テスト用に `vitest` `@testing-library/react` `@playwright/test`。アニメーションは CSS のみ（motion ライブラリは入れない）。アイコンは `lucide-react` 可。
- 状態管理は今の方式（fetch ＋ React state）を `api/hooks.ts` に寄せる。TanStack Query の導入は任意（入れるなら G1 で一括）。
- 既存の機能（今日の計画、ブレインダンプ、候補承認、ゴール、ひらめき、設定、招待、エクスポート、バックアップ）は**消さずに**新しい場所へ移す。対応表を PR 説明に書く。

### 5.2 部品の仕様（モックのクラス → 部品）

| 部品 | モック | 要点 |
|---|---|---|
| `Button` | `.btn .btn-sun .btn-ghost .btn-sm` | variant `sun|ghost|quiet`、size `md(48)|sm(44)`。押下 `:active` で沈む |
| `Card` | `.card` | 枠 2px＋厚み影。`tone="sun"` で枠と影がおひさま（つぎの一手） |
| `Chip` | `.chip .c-*` | tone `xp|coin|sky|berry|grape|plain` |
| `CheckButton` | `.check .check-done .check-boss` | 44px 角丸 14px。完了でわかば＋白チェック。`aria-label="クリアにする"` / `"クリア済み。元に戻す"` |
| `QuestRow` | `.row` | チェック・タイトル・チップ列・右端に +XP。行クリックで詳細。大物は「小分けにする」 |
| `XpBar` / `ProgressBar` | `.bar .bar-fill` | 溝 `--surface-sunk`、中身わかば（マップはそら）、内側下に 4px の影 |
| `ChestProgress` | PcToday の宝箱行 | 進み具合バー＋宝箱 SVG＋「あと N つで宝箱」 |
| `BuddyBubble` | `.bubble` | 相棒 SVG＋白い吹き出し、2 行まで |
| `MapPath` | `.node .n-done .n-now .n-lock .stage` | §3.8 |
| `TabBar` | SpToday 下部 | 5 枠、中央の＋は 60px・角丸 22px・上に 26px はみ出す |
| `ClearCelebration` | SpClear | §3.5 |
| `Segmented` | 依頼ボードの `.seg` | `role="tablist"` |

### 5.3 文言の規則

- 「AI」「✦」「生成」「分析」を通常画面に出さない（設定の連携画面と、AI が使えないときの理由表示だけは可）。
- 相棒の文はです・ます調にしない（`tone` 設定に従う。既定「やさしい」＝ため口のやさしい言葉）。
- 期限切れ・失敗を責める語（「遅れています」「未達」）を使わない。
- 数字は「3つ」「+12 XP」「340 G」の形にそろえる。

---

## 6. PR の分け方と受け入れ条件

各 PR は単独でビルド・テストが通ること。`python -m pytest` 全件、`cd web && npm run build`、`npm run test:device` を毎回実行し、結果を PR 説明に貼る。UI の PR はスマホ 390×844 と PC 1440×900 のスクリーンショット（ライト・ダーク）を添付。

### G0 土台（見た目は変えない）
- `App.tsx` を §5.1 の構成に分割（既存の `screens/`・`components/TrashPanel.tsx`・`GoalsView.tsx` は §5.1 の場所へ移すだけでよい）。`react-router-dom` を入れ、画面を URL で開けるようにする（`/today` `/map` `/board` `/me` `/me/settings`、旧ハッシュやパスからの転送）。
- Vitest と Testing Library を入れ、`QuestRow` と `api/client.ts` に最初のテスト。
- 受け入れ: 既存の全操作が分割前と同じに動く（ゴミ箱、招待、モバイルログイン、iOS の共有からの取り込みを含む）。`App.tsx` は 200 行以下。

### G1 トークンと部品
- `tokens.game.css` を適用、フォント同梱、`base.css` を新トークンで書き直す。§5.2 の部品と `Buddy` を作る。
- `/dev/kit`（開発ビルドのみ）に部品一覧ページ。
- 受け入れ: 部品一覧がモック `Concept.dc.html` と見た目一致。ライト・ダークとも本文コントラスト 4.5:1 以上（axe で確認）。✦ と絵文字が UI から消えている。

### G2 ゲームの API（バックエンドのみ）
- §4.2 v18（SQLite・PostgreSQL）、§4.3、§4.5 の `player` / `today` 拡張 / `complete` 拡張 / achievements / rewards / history。
- 受け入れ: 報酬の二重付与なし（完了→戻す→再完了で 1 回）、デイリークリアは 1 日 1 回、triage は 1 日 20 件まで、おやすみ券の表テスト、v17 DB（SQLite・PostgreSQL）からの移行テスト。既存テスト全件成功。

### G3 きょうの冒険（PC・スマホ）
- `PcToday` `SpToday` のとおり。相棒のひとこと、つぎの一手（はじめる＝既存フォーカス、もうクリアした、小分けにする）、進み具合と宝箱、きょうのクエスト（体力メーター）、持ち越し（きょうやる・日を変える・スワイプ）、クリア済みの折りたたみ、PC 右列。
- 作戦会議カード（きょう 0 件のとき。既存 `POST /api/today/plan` の推薦 3 件）とふりかえりカード（18 時以降）。
- 受け入れ: 開いて 3 秒で「つぎの一手」が分かる。1 タップでクリア。

### G4 クリアの手応え
- §3.5 の 2 段階演出、XP の浮き上がり、バーの伸び、宝箱、レベルアップ・実績解除の全面演出、元に戻す、音（既定オフ）・振動。
- 受け入れ: reduced-motion で演出が止まる。演出中も次の操作をブロックしない（全面画面は「つぎのクエストへ」で閉じる、Esc / 下スワイプでも閉じる）。

### G5 ミナに渡す・依頼ボード
- `SpCapture` と `SpInbox`。1 行と複数行の自動切替、分割プレビュー、入口ボタン。依頼・メモ・AI の報告の 3 タブ、受ける/見送る/直して受ける、triage ボーナス表示。
- 受け入れ: ネット・API キー無しでも 1 行入力と分割（ルール版）が動く。

### G6 たね と 冒険マップ
- §4.4、§4.5 の seeds / maps API。`PcMap` `SpMap` `SpSeed`。育てる 3 ステップ、地図の案の編集（ステージ名・クエストの外し・追加）、採用、次のステージを描く、放置のひと休み表示。
- 受け入れ: たね → 採用 → きょうに 1 件入る までがスマホで 1 分以内。キャンセルしても DB に何も残らない。AI 無しでもフォールバックで地図が作れる。

### G7 じぶん
- `SpMe`。プレイヤーカード、あしあと（28 日、おやすみ券の日は点線）、実績（未解除はロック）、ごほうびショップ（追加・交換・不足時の表示）、冒険日誌（週次ふりかえり）、設定（相棒の名前・口調・呼び名・音、テーマ、既存の連携・データ・招待）。

### G8 仕上げ
- Playwright の E2E（390 / 1024 / 1440 幅）: 完了→演出→元に戻す、ミナに渡す（複数行）、依頼を受ける、たね→地図→きょう。
- PWA の manifest アイコンを新しい相棒に差し替え、`theme_color` を `#FFC23D` 系に。
- 旧トークン・未使用 CSS の削除。

---

## 7. やらないこと（今回の範囲外）

- 対戦・ランキング・ソーシャル共有（個人アプリなので入れない）。
- 課金・ガチャ的な仕組み（ゴールドは自分で決めたご褒美とだけ交換する）。
- Streamlit 版の見た目変更（並行稼働のため触らない。ロジックの共通化で結果が変わる箇所だけテストで確認）。
- `items` テーブルの列名変更や `title` 専用列の追加。
