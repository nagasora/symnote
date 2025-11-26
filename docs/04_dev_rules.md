# SymNote 開発ルール v0.1

## 1. コード規約
- **型ヒント**: すべての関数・クラスに完全な型ヒントを付与。List/Tuple/Optional など typing を積極的に使用。
- **Docstring**: 関数の Docstring は原則日本語で記載。何をするか・引数・戻り値を明示。
- **関数/クラス設計**: 処理は関数/クラスへ分割し、1関数は目安50行以内。
- **コメントポリシー**: 「Why not」を書く（なぜこの実装か、他案を採用しなかった理由）。
  ```python
  # Why not: importance×urgency だけでは偏るため energy も加味
  ```

## 2. 開発プロセス
- **ブランチ戦略**: `main`（安定） / `develop`（開発） / `feature/*`（機能単位）。
- **コミット規約**: プレフィックスを付与し、メッセージに Why を含める。`feat`, `fix`, `refactor`, `test`, `docs`, `chore`
  ```text
  feat: add inbox AI classifier (Why: enable automatic task extraction)
  ```

## 3. テスト規約
- **フレームワーク**: pytest。
- **書き方ルール**: 「何を検証するか」をコメントで明示。
- **単体テスト**: `core/db.py`（insert/fetch/update）、`core/nlp.py`（AI 結果の形）、`core/priority.py`（優先度計算）。
- **結合テスト**: インボックス → タスク化のフロー。
- **E2E（時間があれば）**: `app.py` 起動と基本画面遷移。

## 4. Lint / Format
- ruff（推奨）、black。※導入前は手動整形でも可。

## 5. 開発サイクル
- 要件 → issue 化 → feature ブランチ開発 → テスト → PR / チェック → `develop` マージ → 定期的に `main` リリース。

## 6. データ保護
- DB ファイル `symnote.db` は `.gitignore` で追跡しない。
- 週次でバックアップを取得。
- マイグレーションは SQLAlchemy の `ALTER TABLE` で対応。

## 7. 推奨日次フロー
- インボックスに「今日のタスク」を SymNote に記録。
- タスク整理画面で実装タスクを可視化。
- Today View で「今日やること」を決定。
- 1日終わりに「今日の振り返り」を入力。
- 土曜に週次 AI レポート生成。
