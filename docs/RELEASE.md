# SymNote 招待制ベータ リリース & Cloudflare 公開手順書

> これはMac常駐版の既存手順です。iOS・Webの二系統リリース方針は [IOS_WEB_RELEASE.md](IOS_WEB_RELEASE.md)、Mac非依存のWeb基盤は [POSTGRESQL.md](POSTGRESQL.md) を参照してください。

このドキュメントでは、Mac 上で動く **SymNote Web（招待制ベータ版）** を **Cloudflare Tunnel + 独自ドメイン + Google ログイン** で安全に外部公開し、スマホ（iPhone / Android）や PC、招待した友人がブラウザ・ホーム画面アプリ（PWA）から使えるようにする手順を説明します。

---

## 全体の仕組み（セキュリティとデータ分離）

1. **ポート開放不要**: `symnote-web` は Mac 内部の `127.0.0.1:8000` のみで待ち受け、Cloudflare Tunnel（`cloudflared`）が HTTPS（`https://あなたのドメイン`）で中継します。自宅ルーターのポート開放は一切不要です。
2. **招待リスト（Allowlist）方式の Google ログイン**: `symnote-admin invite`（または Web の「設定」画面）で招待された Google メールアドレスだけがログインできます。未招待の Google アカウントは自動で拒否されます。
3. **利用者ごとの完全な SQLite 分離**:
   - **オーナー（あなた）**: これまで通りの `DB_PATH`（既存のタスク・メモ・ゴール）をそのまま引き継ぎます。
   - **招待メンバー**: 初回ログイン時に `data/users/<user_id>/symnote.db`（パーミッション `600`）が自動作成され、あなたや他の招待メンバーのデータとは物理的に別ファイルとして完全分離されます。
4. **AI 利用量の上限保護**: 1人あたり1日200回・1分10回、システム全体で1日1,000回の上限（`system.db` 管理）により、万一の API 使いすぎを防ぎます。

---

## ステップ 1: Cloudflare でのドメイン準備（ブラウザ操作）

1. [Cloudflare ダッシュボード](https://dash.cloudflare.com/) にログインします（アカウントがなければ無料作成）。
2. **ドメインを用意する**:
   - **新しく取得する場合**: 左メニュー **「Domain Registration（ドメイン登録）」→「Register Domains（ドメインの登録）」** から検索して購入します。候補は `symnote.app`、`getsymnote.com`、`symnote-app.com` の順です。空き状況・初年度料金・更新料金は購入画面で確認してください（この手順書では未確認）。Cloudflare で購入するとネームサーバー設定が最初から完了しています。
   - **既存ドメインを使う場合**: 左メニュー **「Websites」→「Add a site（サイトを追加）」** からドメインを入力し、Free プランを選択して表示される2つのネームサーバー（`xxx.ns.cloudflare.com`）をお使いのレジストラ（お名前.com / Google Domains / Squarespace 等）に設定します。
3. ドメインのステータスが **「Active（有効）」** になっていることを確認します。

ドメインは1つ購入すれば十分です。たとえば `getsymnote.com` を取得した場合、アプリは `app.getsymnote.com` で公開し、ルートは将来の紹介ページに使えます。以下の手順の公開ホスト名・OAuth URL・環境変数は、すべて同じホスト名に揃えてください。

---

## ステップ 2: Mac で Cloudflare Tunnel を作成・DNS 紐付け（ターミナル操作）

1. `cloudflared` が入っていなければ Homebrew でインストールします:
   ```bash
   brew install cloudflared
   ```
2. Cloudflare にログインして証明書を取得します（ブラウザが開くので、ステップ1のドメインを選択して **Authorize** を押します）:
   ```bash
   cloudflared tunnel login
   ```
3. `symnote` という名前の固定トンネルを作成します:
   ```bash
   cloudflared tunnel create symnote
   ```
   実行すると `Created tunnel symnote with id xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` と表示され、`~/.cloudflared/<TUNNEL_ID>.json` が生成されます。この `<TUNNEL_ID>` を控えておきます。
4. 公開したいサブドメイン（例: `app.symnote.app`）へ DNS ルートを自動作成します:
   ```bash
   cloudflared tunnel route dns symnote app.symnote.app
   ```
   （※ Cloudflare ダッシュボードの「DNS → Records」に CNAME レコードが自動で追加されます）
5. `~/.cloudflared/config.yml` を作成します:
   ```yaml
   tunnel: <ステップ3で表示された TUNNEL_ID>
   credentials-file: /Users/<あなたのMacユーザー名>/.cloudflared/<TUNNEL_ID>.json

   ingress:
     - hostname: app.symnote.app
       service: http://127.0.0.1:8000
     - service: http_status:404
   ```

---

## ステップ 3: Google Cloud で Google ログイン（OAuth 2.0）を設定

1. [Google Cloud Console](https://console.cloud.google.com/) を開き、プロジェクトを選択（または作成）します。
2. **「API とサービス」→「OAuth 同意画面」**:
   - User Type: **外部（External）**
   - アプリ名: `SymNote`
   - スコープ: `openid`, `.../auth/userinfo.email`, `.../auth/userinfo.profile`（標準の基本情報のみなので Google による有料審査は不要です）
   - 公開ステータスが「テスト中」の場合は「テストユーザー」に招待したい人の Gmail を追加するか、「アプリを公開」ボタンを押して本番化します（`openid email profile` のみであれば審査なしで即時公開できます）。
3. **「API とサービス」→「認証情報」→「＋ 認証情報を作成」→「OAuth クライアント ID」**:
   - アプリケーションの種類: **ウェブ アプリケーション**
   - 名前: `SymNote Web`
   - **承認済みの JavaScript 生成元**:
     - `https://app.symnote.app`
   - **承認済みのリダイレクト URI**:
     - `https://app.symnote.app/api/auth/google/callback`
4. 作成後に表示される **クライアント ID** と **クライアント シークレット** を控えます。

---

## ステップ 4: `.env` の設定とオーナー・招待メンバー登録

1. このチェックアウトでは既存の `/Users/nagamatsusoukuu/symnote/src/symnote/.env` に以下の設定を追記します。既存の `DB_PATH`・APIキー・カレンダー設定は保持し、編集前に所有者だけが読めるバックアップを作成してください。`load_dotenv()` はこのファイルを先に見つけるため、リポジトリ直下へ別の `.env` を作成するだけでは設定が反映されません:
   ```dotenv
   # あなたの Google アカウント（既存の DB_PATH を引き継ぐオーナー）
   SYMNOTE_OWNER_EMAIL=あなたのアドレス@gmail.com

   # 公開ドメイン（Host ヘッダー検証・OAuth リダイレクトに使用）
   SYMNOTE_PUBLIC_ORIGIN=https://app.symnote.app
   SYMNOTE_ALLOWED_HOSTS=app.symnote.app,127.0.0.1,localhost

   # ステップ3で取得した Google OAuth クライアント情報
   SYMNOTE_GOOGLE_CLIENT_ID=xxxx.apps.googleusercontent.com
   SYMNOTE_GOOGLE_CLIENT_SECRET=GOCSPX-xxxxxxxx
   ```
   `SYMNOTE_ALLOWED_HOSTS` はスキームやパスを含まないホスト名の一覧です。Googleログインには短時間のブラウザ確認Cookieを使うため、ログイン開始から完了まで同じブラウザ・同じホスト名を使ってください。
2. オーナーと招待したい友人のメールアドレスを登録します（Web 画面の「設定」からも追加・取消できます）:
   ```bash
   cd /Users/nagamatsusoukuu/symnote
   .venv/bin/python -m symnote.admin_cli invite あなたのアドレス@gmail.com --owner
   .venv/bin/python -m symnote.admin_cli invite friend@gmail.com --note "ベータ招待"
   .venv/bin/python -m symnote.admin_cli list
   ```

---

## ステップ 5: ビルドと起動（& 自動起動設定）

1. フロントエンドをビルドします:
   ```bash
   cd /Users/nagamatsusoukuu/symnote/web
   npm install && npm run build
   ```
2. Web サーバーを起動します:
   ```bash
   cd /Users/nagamatsusoukuu/symnote
   .venv/bin/python -m symnote.web_server --host 127.0.0.1 --port 8000
   ```
3. 別ターミナルで Cloudflare Tunnel を起動します:
   ```bash
   cloudflared tunnel run symnote
   ```
4. スマホや PC から `https://app.symnote.app` を開き、「Google アカウントでログイン」を押してログインできることを確認します。
   - **iPhone (Safari)**: 共有ボタン → **「ホーム画面に追加」** でネイティブアプリのように全画面で使えます。
   - **Mac 再起動時も自動で立ち上げる場合**: `docs/launchd/com.symnote.web.plist.example` と `docs/launchd/com.symnote.cloudflared.plist.example` を `~/Library/LaunchAgents/` にコピーして `launchctl load -w` してください。

## 公開後の確認

- スマホのモバイル回線から開き、未ログインではタスクを取得できないことを確認する。
- 招待済みのGoogleアカウントでログインし、タスクの追加・完了・取り消しを確認する。
- 未招待のアカウントがログインを拒否されることを確認する。
- 別の招待メンバーから、オーナーのタスクやメモが見えないことを確認する。
- Macの電源とネットワーク接続が必要。Macがスリープしている間は利用できないため、常時提供する際は稼働環境を別途用意する。

このWeb版は招待制ベータです。有料の一般公開には、課金・契約管理などが別途必要です。Googleログインの設定は、Googleカレンダーの接続設定とは別です。
