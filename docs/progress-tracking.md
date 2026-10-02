# プロジェクト進捗の使い方

SymNote 内に **プロジェクト → 目標 → ToDo** を作り、現在地・次の一手・担当者・停止理由・承認状況を記録します。「今日」には進行中の目標から次の一手を1件表示し、「進捗レビュー」にはエージェント報告、古い報告、版の競合、完了申告と追記履歴を表示します。

「個人」と「ワークスペース」は同じ端末内で整理するためのラベルです。ログインや共有アクセス制御を提供するものではありません。

## エージェントから報告する

Claude Code など既存の MCP 接続では、`list_projects` と `list_project_progress` で読んでから `report_progress` にイベントを渡せます。`report_progress` は claim を追記するだけです。既存の MCP 接続やエージェント設定がない環境では、標準ライブラリだけのローカル reporter で JSONL ファイルを作り、SymNote の「進捗レビュー」から手動で取り込みます。

次の例の ID、project key、commit、参照先はすべて置き換えてください。

```bash
PYTHONPATH=/absolute/path/to/symnote/src \
  /absolute/path/to/symnote/.venv/bin/python -m symnote.progress_reporter \
  --output ./progress.jsonl \
  --project-key sample-project \
  --target-kind task --target-id 12 --expected-revision 0 \
  --status completed \
  --current "実装を終えた。レビュー待ち。" \
  --next-action "SymNote の進捗レビューで成果物を確認する。" \
  --repo https://example.test/team/project --branch feature/example \
  --commit 0123456789abcdef0123456789abcdef01234567 \
  --artifact-ref artifacts/change-summary.txt \
  --test-ref artifacts/test-report.txt --test-result passed
```

目標や ToDo の ID、revision、project key は UI または MCP の読み取り結果から確認します。JSONL は複数行をまとめて取り込めます。イベント ID と内容が同じ再取り込みは重複として無視し、同じ ID で内容が変わったファイルはエラーにします。取り込みは一括トランザクションで、1件でも不正なら全体を取り消します。

### 報告と証拠の扱い

- `completed`、テスト結果、artifact/test reference はエージェントによる申告です。取り込んでも ToDo の状態は完了になりません。
- report は JSON データとして保存します。参照先を開いたり、文字列中のコマンドを実行したりしません。
- 取り込んだ時刻を別に記録し、報告が7日以上ない場合は「更新なし」と表示します。
- 同じ revision を元にした報告が先に取り込まれていたら、後の報告を `conflict` として履歴に残し、状態を上書きしません。
- 完了申告を verified として記録するには、commit・成果物参照・テスト参照・`passed` の申告が必要です。確認者が同じ報告をレビューし、成果物と同じ commit のテスト根拠を確認したチェックを記録します。SymNote はリンクを開いてテストを再実行しません。
- review は元のイベント ID に結び付くため、別 commit の報告を認定しません。verified は人の確認記録であり、機械的な保証ではありません。

## 保存と端末の範囲

新しい進捗テーブルは SQLite の schema migration 11 で追加されます。旧版の DB から更新するときは、migration 前に SQLite Backup API で `backups/` にローカルバックアップを作成します。バックアップには DB 内の個人データが含まれるため、DB と同じ注意で保護してください。「進捗レビュー」から進捗ログを JSONL に書き出せます。必要なら既存の DB backup 機能も利用してください。

アプリはローカル利用を前提にし、スマートフォンからのリモート接続や公開ホスティングはこの変更では設定していません。モバイル幅で画面を確認できますが、実機スマートフォンからのアクセスは未確認です。実際に外から接続するには、認証付きのホスティングまたは適切に保護したネットワーク構成が別途必要です。

`.env` と SQLite ファイルは `.gitignore` 対象です。`.env.example` は空の API キー欄と例示値だけを含む設定テンプレートです。過去に公開履歴へ認証情報が含まれていた可能性がある場合は、所有者が認証情報の失効・交換と履歴の対応を別途判断してください。
