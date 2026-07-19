# 現状監査とローカル永続化ロードマップ

最終更新: 2026-07-19

## 前提

SymNote は個人利用のローカルファーストアプリとして扱う。ノート、タスク、添付文書、生成結果はユーザー所有のローカルデータであり、ネットワークや AI API が利用できない場合も閲覧・編集できる状態を維持する。

## P0: 直ちに対応する項目

1. **漏えい済み API キーの失効と再発行**
   - Git 追跡済みの `src/symnote/.env` に設定済みキーが含まれている。
   - `.gitignore` と `.dockerignore` による再発防止は追加済み。
   - キーの失効・再発行と Git 履歴からの除去は、外部サービスと履歴を書き換えるため別作業として実施する。
2. **ユーザーデータを Git 管理から分離**
   - `symnote.db` と生成済み `pyc` が現在も追跡対象になっている。ignore は追跡済みファイルには遡及しない。
   - 作業中 DB のバックアップを確認してから、Git の追跡だけを解除する。
3. **期限モデルの統一**
   - UI は期限入力を `date` に保存する一方、カレンダーは `due_date` を参照している。
   - `created_at`、対象日、期限日を定義し、保存・Today・Calendar・AI 推薦を同じ意味に統一する。
4. **回帰テストの導入**
   - 現在の `tests/` 配下は空で、DB や分類ロジックの回帰を検知できない。

## P1: ローカル永続運用に必要な項目

1. **固定データディレクトリ**
   - 相対パス `./symnote.db` を廃止し、Windows では `%LOCALAPPDATA%/SymNote/data/` などの固定場所を既定にする。
   - 実際の保存先を設定画面に表示し、`DB_PATH` による明示的な上書きは残す。
2. **SQLite の耐久性**
   - 接続を必ず close する context manager を導入する。
   - `busy_timeout`、WAL、`foreign_keys=ON`、起動時の軽量な整合性検査を設定する。
   - `PRAGMA user_version` または migration ledger による連番マイグレーションへ移行する。
3. **バックアップと復元**
   - SQLite Backup API を使い、日次・週次の世代バックアップを作る。
   - バックアップ後の `integrity_check`、復元前スナップショット、復元 dry-run を用意する。
   - 可搬性のため JSON/CSV エクスポートも用意する。
4. **誤削除対策と参照整合性**
   - items、idea sessions、mindmap nodes に soft delete とゴミ箱を導入する。
   - mindmap の親子・セッション・リンク参照に外部キーと削除方針を定義する。
5. **オフライン劣化動作**
   - AI provider interface を設け、コア操作と外部 Gemini 呼び出しを分離する。
   - API キーやネットワークがない場合も、既存データの閲覧・編集とルールベース処理を継続する。
   - 将来的に Ollama または llama.cpp 系のローカル provider を選択可能にする。
6. **Docker のデータ分離**
   - localhost のみに公開し、ソース全体の bind mount ではなく専用 volume に DB を保存する。
   - 秘密情報・DB・仮想環境を build context から除外する。

## P2: 保守性と機能完成度

- 約 680 行の `app.py` を view/component/use-case 単位へ分割する。
- AI 応答を構造化検証し、timeout、retry、利用者に見えるエラー表示を追加する。
- idea chat、ToDo 生成、weekly review の未接続導線を完成させるか、不要な死コードを整理する。
- mindmap の `depth` 更新とセッション削除時の孤児ノードを修正する。
- 未使用の重量級依存を整理し、再現可能な lock とオフライン用 wheel/model 保管手順を検討する。
- README とコード内コメントの文字化けを修復し、実装とドキュメントの差異を解消する。

## 今回整備した開発基盤

- `.codex/config.toml`: 同時エージェント数、深さ、実行時間の上限。
- `.codex/agents/*.toml`: architecture、persistence、quality のプロジェクト専用監査役。
- `AGENTS.md`: ローカルファースト制約と安全な並列作業ルール。
- `pyproject.toml`: Python パッケージ、pytest、ruff の共通設定。
- `requirements.txt`: 現行コードで使用する最小ランタイム依存へ整理。
- `.gitignore` / `.dockerignore`: 秘密情報、DB、仮想環境、生成物の除外。
- `docker-compose.yml` / `docker/entrypoint.sh`: localhost 公開、データ volume、起動スクリプトの修正。

## 検証結果

- TOML 5 ファイル: 構文検証成功。
- Docker Compose: `docker compose config --quiet` 成功。
- 仮想環境: Python 3.12.4 で復旧し、editable install と `pip check` 成功。
- コアモジュール: import 成功。
- pytest: 設定は読み込めたが、テスト 0 件のため終了コード 1。
- ruff: 147 件（うち自動修正可能 88 件）。未定義の `Any`、未使用 import、型注釈、行長、書式が中心。

## 今回の改善実装

- Codexの既定モデルとプロジェクトエージェントを `gpt-5.6-terra` に設定。
- UI、Today、Calendar、NLP推薦の期限判定を `due_date` に統一。
- 既存タスクの `date` を `due_date` に補完する `user_version` 1→3 マイグレーションを追加。
- DB既定保存先をユーザーデータ領域へ移し、明示的な `DB_PATH` は引き続き利用可能にした。
- SQLiteのWAL、外部キー、busy timeout、接続コンテキスト、Backup API、integrity checkを追加。
- 週次レビューは週内に作成または期限到来するタスクを対象にした。
- DB/NLP/マインドマップの回帰テストを追加し、最終検証は11件成功。
- DB公開関数の接続をコンテキスト管理へ統一し、マインドマップ保存を単一トランザクション化。

## レビュー後も残る項目

- `.env`、DB、pycはGit追跡済みのため、キー失効・追跡解除・履歴対策が必要。
- 自動バックアップはアプリ起動経路へ未接続で、現状は安全な手動Backup APIヘルパー。
- Git履歴対策、AI応答の構造化検証、ruff指摘の解消は未完了。
- ruffの既存指摘、Calendarの重複集計表示、マインドマップの一括保存は次の改善対象。
