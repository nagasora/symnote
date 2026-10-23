# Gmail・Slack直接連携と実機テストの前提

2026-10-23。基本機能はSymNote自身が情報源を読み、既存AI APIで承認待ち候補を作る。ChatGPT Work/MCP経由の候補送信は任意の追加経路とし、直接連携には不要。

## 実装済み

- 利用者別OAuth。ブラウザセッション・利用者・プロバイダーに結び付けた一回限りのstate。有効期限10分。GmailはPKCEも使用。
- 利用者別DB／PostgreSQLスキーマに暗号化接続情報を保存。暗号化された内容にも本人とプロバイダーを結び付け、別利用者へコピーしたデータを拒否。
- Gmailは`gmail.readonly`だけ。メイン受信箱（INBOX・CATEGORY_PERSONAL）の直近7日間を読み、送信・既読化・ラベル変更・削除は行わない。
- Slackは`user_scope=channels:read,channels:history`だけ。Botの機能・権限は要求せず、Botを含む応答や追加権限も拒否。チャンネル参加・招待・投稿のAPIは通信入口でも拒否する。
- 接続後、本人が公開チャンネルを最大5つ選ぶ。指定前は本文を取得しない。DM・非公開チャンネル・添付ファイル・スレッド返信の別途取得は対象外。Slack側の管理・認可情報の表示を隠す機能ではない。
- 各取り込みは、指定範囲を設定済みAIへ送信することに本人が同意して開始する。候補・説明・根拠引用を自分の依頼ボードへ保存し、本人が「受ける」を押すまでタスクにしない。
- 接続確認・取得確認・全範囲の取り込み完了を別々の日時で表示する。設定ありだけで実接続成功と表示しない。
- 既存のAI利用枠、取得上限、429とRetry-Afterの表示、トークンの期限・更新、ローカル接続解除を実装。解除はSymNote内の資格情報を削除する操作で、プロバイダー側の許可取消は各サービスで行う。
- 同じ情報源の並行操作を拒否し、実行権限を外部通信／AI送信直前に確認する。認可コード交換中に解除・再開始された古いcallbackは保存できない。
- 成功した出典だけを候補と同じトランザクションで処理済みにする。失敗した出典は再試行できる。Gmailは1回最大20件、Slackは1回1チャンネルの1ページ・最大15件。続きの位置は成功後だけ保存し、次回へ引き継ぐ。Slackの続きは短時間で期限切れになるカーソルではなく、取得済みの最古の投稿時刻を境界に再開する。Gmailの期限切れページ位置は処理済み照合を保って先頭から再走査する。複数チャンネルは順番に読む。未取得部分は完了扱いにしない。
- 会話テキストは最大12,000文字の明示送信に対応。ChatGPTの全履歴自動取得やファイルアップロードは未対応。

## 設定

既存Googleログインの設定`SYMNOTE_GOOGLE_CLIENT_ID`／`SYMNOTE_GOOGLE_CLIENT_SECRET`を使用する。

追加のサーバー設定：

|変数|用途|
|---|---|
|`SYMNOTE_PUBLIC_ORIGIN`|固定HTTPS origin。現在の戻り先は`https://app.symnote.app`。|
|`SYMNOTE_SLACK_CLIENT_ID` / `SYMNOTE_SLACK_CLIENT_SECRET`|BotのないSlackアプリのOAuth設定。|
|`SYMNOTE_INTEGRATION_KEY`|Fernet形式の接続情報暗号鍵。DBとは別に安全に保管する。|

本人承認に基づき、ローカル暗号鍵を`.env.integrations.local`へ保存済み（0600）。既存`.env`は編集していない。起動時にこの追加ファイルを読み、環境変数や既存`.env`の指定を上書きしない。Git／Dockerの送信対象から除外。鍵を紛失・変更すると既存接続を復号できなくなるため、サーバーでは秘密設定として継続保管する。ファイルや秘密値をチャットへ貼らない。

```sh
.venv/bin/python scripts/check_source_integrations.py
```

このコマンドは設定の存在と固定callback URLだけを出力する。資格情報・実DBは出力せず、未設定があれば終了コード1。Google側の登録状況・実OAuth成功まで保証する検査ではない。

Slackアプリ作成用の[manifest](slack-app-manifest.json)にはUser Token Scopesだけを記載し、Bot User・Bot Scopes・Events・Webhookは含めない。対象ワークスペースは本人指定の「松尾研 DL community」。アプリの登録と認可にはワークスペースの管理方針に応じた承認が必要。

callback URL：

- `https://app.symnote.app/api/integrations/gmail/callback`
- `https://app.symnote.app/api/integrations/slack/callback`

## API

|メソッド・パス|操作|
|---|---|
|GET `/api/integrations`|本人の接続・取り込み状態。秘密値は返さない。|
|POST `/api/integrations/{provider}/start`|認証済みブラウザから認可開始。|
|GET `/api/integrations/{provider}/callback`|一回限りの認可完了。|
|GET / PUT `/api/integrations/slack/channels`|公開チャンネル一覧／選択を保存。|
|POST `/api/integrations/{provider}/import`|`consent: true`で指定範囲から候補を作成。|
|DELETE `/api/integrations/{provider}`|本人のローカル接続と保留中stateを削除。|
|POST `/api/integrations/conversation`|`text`と`consent: true`で会話から候補作成。|

全操作に既存の認証・利用者スコープを適用。状態変更にはCSRFヘッダーが必要。OAuth接続開始はSafari等のCookieログイン済みブラウザから行う。ネイティブiOSアプリ内の別ブラウザへのセッション引き継ぎは未対応で、最初の実機確認はSafari／PWAとする。

## 移行と検証

SQLite・PostgreSQLへv19の接続・state・処理済み表と、旧v19形式の不足項目を補うv20移行を追加する。既存のメモやタスクを削除・上書きしない。PostgreSQLのインポート／エクスポートでは接続情報と保留中stateを除外し、復元先へ秘密値を運ばない。

自動テストは一時SQLiteと使い捨てPostgreSQLで行う。認証・同意・CSRF、本人／別利用者／別セッション、state再利用、暗号化・取り違え、Bot／追加権限拒否、取得範囲、レート制限、失敗後再試行、ページの引き継ぎ、二重処理、解除・期限切れ競合、移行・エクスポート除外を確認する。外部サービスは模擬通信。全体919件成功・2件の想定スキップを確認し、ページ再開の最終修正後は両DBの連携重点14件が成功。フロント13件・実機準備2件・画面E2E6件とビルドも成功。以前の架空会話1件によるOpenAI実疎通は成功しているが、実Gmail／Slack取得の代替にはしない。

## 外部で実施済み／未完了

- 既存Googleプロジェクト`gen-lang-client-0779756169`（symnote）でGmail APIを有効化し「有効」を確認。
- 本人の保存直前承認を受け、「SymNote Web」にGmail callbackを追加・保存し、保存成功表示を確認。
- OAuth同意画面はテスト中、現在のテスト利用者は1人。一般提供へのGoogle側の検証は別工程。
- Slackは本人の指定により一旦保留。クライアント設定・実接続は行っていない。
- 既存の検証用ホストを再起動してコードを反映。ローカルhealth 200と公開URLでの既存Googleログイン成功を確認。本人の承認後、Gmail OAuthの実認可・接続先照合・接続保存に成功。初回は既存の旧v19テーブルにconsumed列がなく失敗したため、データを保持するv20追加移行を実装し、SQLite／PostgreSQLの移行と認可テスト12件を確認した。本人の同意後、直近7日間のメイン受信箱に限定して実取得を実行したが、対象の未処理メールは0件。候補0件・OpenAI送信0回で、実メールによるAI抽出・重複防止は未確認。対象範囲は広げていない。画面に未処理取得件数とAI処理回数を追加し、API重点4件・フロント13件・ビルド成功を確認。実機テスト完了とも扱わない。

実機への進行条件は「指定した実データで取得→AI候補→本人承認→再取得で重複なし」を確認すること。その後、認証付きHTTPS環境へスマホSafari／PWAでアクセスし、ログイン・承認・クリア・復帰を確認する。既存公開サービスを無断で別の一時DBへ切り替えない。Mac停止中の利用には別途クラウドAPI／DB稼働が必要。

参考：[Google Web OAuth](https://developers.google.com/identity/protocols/oauth2/web-server)、[Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)、[Slack OAuth](https://docs.slack.dev/authentication/installing-with-oauth/)、[Slack history](https://docs.slack.dev/reference/methods/conversations.history/)。
