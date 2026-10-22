# デザインと動作の検証

2026-10-23。CODEX_GAME_REDESIGN.md と同梱HTMLモック・トークンを基準に確認。

## 自動検証

- Python: SQLiteと一時PostgreSQL 18を使用し **901 passed, 2 skipped**。2件のskipはSQLite側で実行しないPostgreSQL専用条件。既存Starletteの非推奨警告が1件。
- Vitest: **11 passed**。APIエラー/壊れたJSON、クエスト操作の分離、ローカル日付、計画/採用中の競合を確認。
- device tests: **2 passed**。
- Web: TypeScriptとViteの本番ビルド成功。
- iOS: Capacitor同期と署名なしシミュレータービルド成功。
- Playwright: **6 passed**（390×844、1024×900、1440×900、それぞれライト・ダーク）。完了→演出→取り消し、複数行入力、依頼承認/失敗再試行、たね採用→今日、次ステージ採用、メモ作成/編集、ゴール休止/再開、任意プレイヤーAPIの障害を確認。
- axe: 各条件で今日・マップ・依頼・じぶん・設定・カレンダー・メモ・ゴール管理・ひらめきの9ルートに違反なし。
- 独立レビュー後の修正を再確認。git diff --check成功。

## スクリーンショット

|画面|スマホ light / dark|PC light / dark|
|---|---|---|
|今日|[light](verification/today-390-light.png) / [dark](verification/today-390-dark.png)|[light](verification/today-1440-light.png) / [dark](verification/today-1440-dark.png)|
|マップ|[light](verification/map-390-light.png) / [dark](verification/map-390-dark.png)|[light](verification/map-1440-light.png) / [dark](verification/map-1440-dark.png)|
|じぶん|[light](verification/me-390-light.png) / [dark](verification/me-390-dark.png)|[light](verification/me-1440-light.png) / [dark](verification/me-1440-dark.png)|

タブレット: [light](verification/today-1024-light.png) / [dark](verification/today-1024-dark.png)。PCは左ナビと右列、スマホは下部ナビと中央の入力ボタンを使用。横はみ出しなし。フォントは同梱し外部配信に依存しない。ダークテーマの相棒・レベル表示と、スマホカレンダーの文字コントラストも確認した。

## 検証の限界

自動ブラウザはChromium、iOSは署名なしビルドの確認。実機iPhone、Safariでの操作、Google OAuth/カレンダーやAIプロバイダーへの実接続、ホスト版の本番稼働は確認していない。これらをローカル検証の成功として扱わない。
