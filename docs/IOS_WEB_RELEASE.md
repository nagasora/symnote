# SymNote iOS・Web リリース方針

更新: 2026-10-22。Webブラウザ版とApp Storeで配布するiOSアプリを対象とする。PWAをホーム画面へ追加するだけではiOSアプリのリリース完了としない。

## 構成

Webは既存React画面とPython APIを使用し、RenderのWebサービス・PostgreSQLへ移行する。Cloudflareはドメイン/DNSを管理し、常時稼働するMacを必須にしない。ローカルSQLiteと既存ユーザーデータは維持する。

iOSはCapacitor 8のネイティブプロジェクトを追加し、React/TypeScript・デザイン資産を共用する。画面資産をアプリ内に同梱し、本番URLをWebViewで表示するだけの実装にしない。クラウドAPIを同一の業務ロジックへ接続し、端末機能は公式プラグイン/Swiftで補う。APIキーとGoogleクライアントシークレットはサーバーに置き、アプリやVite環境変数へ埋め込まない。

現在のiOS起動画面は端末内メモのみ。Webのタスク・ゴール・認証・クラウドデータはまだ接続していない。Webの既存画面・同一オリジン認証は変更しない。

## 今回実装した範囲

- 仮Bundle ID `app.symnote.mobile`、アプリ名SymNote、Swift Package ManagerによるiOSプロジェクト。Bundle IDのApple Developer登録・利用可能性は未確認。
- IndexedDBによる端末内の単一メモ。明示保存、保存済み/未保存表示、再起動後の読み込み、保存失敗表示。読み込みに失敗した場合は編集をロックし、既存内容を上書きしない。
- iOS標準共有シートへのメモ書き出し。ユーザーが共有先を選ぶまで外部へ自動送信しない。
- 端末メモはクラウドへ送らない。共有は未保存の入力内容も対象とする。アプリ削除時のデータ消失について画面で案内する。端末内メモは既存SQLiteの代替バックアップではない。
- Webビルド、iOS同期、シミュレータ向け未署名ビルドのコマンド。実機署名、TestFlight、App Store審査は別工程。

## 再現手順

Node 22.18以降のTypeScript直接実行に対応する環境と、Capacitor 8の公式要件を満たすXcodeが必要。CLIは依存監査を通る8.4.3、core/iosは8.5.3に固定している。

```sh
cd web
npm ci
npm run test:device
npm run build
npm run ios:build:simulator
npm run ios:open
```

`ios:sync`はWebをビルドして同梱する。生成されたpublic/、マシン依存のxcuserdata、DerivedData、秘密情報はコミットしない。ビルドスクリプトはDEVELOPER_DIRが未指定なら標準のXcode.appを使い、グローバルなxcode-select設定は変更しない。

## リリース順序と完了条件

| 段階 | 実施内容 | 完了条件 |
|---|---|---|
| Web基盤 | Render支払い情報、Web/DB作成、秘密設定、承認済みデータ移行、DNS/TLS | 公開URLでGoogleログイン・AI・2ユーザー分離が動き、Mac停止中に実機から使える |
| iOS認証 | システム認証ブラウザ、Google、Appleログイン、アカウント連携・削除 | 認証情報をURLやJS保存領域に残さず、別アプリへのコード横取り・再利用を拒否する |
| 共通機能と同期 | iOSから既存タスク・メモ・ゴールAPIを使用。端末保存、オフライン編集、再送 | UUID等の操作識別子で重複を防ぎ、競合時に双方の変更を失わず復旧できる |
| iOS体験 | クイック入力、共有受け取り、通知等を必要性に応じて追加 | 通信断・アプリ終了・再起動でも主要操作が使え、Webに加えた端末固有の価値を実機で確認 |
| TestFlight | Apple Developer登録、確定Bundle ID、署名、App Store Connect | 実機インストールと認証・保存・共有・同期の確認、外部テスト要件の充足 |
| 一般公開 | アイコン/スクリーンショット、説明、プライバシー、アカウント削除、審査用アカウント | Web一般公開とApp Store審査承認・配信をそれぞれ確認 |

WebとiOSを別々に公開できるようにし、Webの先行公開から実利用を確認する。iOSはTestFlightで同期・データ保全を確認してから一般公開する。初期は招待制を維持し、一般公開時には登録・退会・運用上限も整える。

## 認証・審査で守ること

Google OAuthをWKWebView内で開かない。iOSではASWebAuthenticationSession等のシステム認証経路を用い、サーバーが検証した一回限りのコードとPKCEでアプリへ戻す。永続セッション/更新トークンはKeychain等の保護領域で扱い、URL、localStorage、端末メモへ保存しない。Web用HttpOnly Cookieをそのまま共有できるとは想定しない。現状のCapacitor画面からGoogleログインを起動しない。

Appleの4.8 Login Servicesに適合する同等のログイン手段としてSign in with Appleを実装する方針。GoogleとAppleのアカウントをメール一致だけで自動統合せず、認証済みの本人による連携を必要とする。Appleの非公開メールでも利用できるID設計とする。4.2 Minimum Functionalityの審査もあるため、ラッパー生成や端末メモだけで審査に通るとは主張しない。

アカウント作成を提供する場合はアプリ内からの削除開始も実装する。AIへメモを送る前の説明・同意、App Privacy申告、Privacy Manifest、利用規約、サポート窓口を実データの流れに合わせる。デジタル機能を有料化する場合は購入方式とApp Storeの現行ルールを別途確認し、現時点で外部決済導線を追加しない。

## 現在の外部ゲート

Render接続とMy Workspaceの選択は確認済み。支払い情報未登録によりDB作成は402で拒否され、DB未作成も確認済み。登録完了の連絡を受けるまでは有料作成を再試行しない。既存の月額構成・データ移行承認は引き継ぐ。Appleのチーム・Bundle ID・署名・配信アカウントは未確認で、新規の有料契約や一般公開を完了扱いしない。

参照: [Capacitor導入](https://capacitorjs.com/docs/getting-started)、[iOS環境](https://capacitorjs.com/docs/getting-started/environment-setup)、[Google OAuthポリシー](https://developers.google.com/identity/protocols/oauth2/policies)、[Apple審査ガイドライン](https://developer.apple.com/app-store/review/guidelines/)。
