import { RemoteSource, type ConnectionStatus } from "./RemoteSource";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { apiFetch } from "../../api/client";
import { Button } from "../../components/ui/Button";
import { Card } from "../../components/ui/Card";

type Status = {
  ai: { configured: boolean; provider: string; connection_verified: boolean };
  conversation: { max_characters: number };
  gmail: ConnectionStatus;
  slack: ConnectionStatus;
};

export function SourceIntegrations() {
  const [status, setStatus] = useState<Status | null>(null);
  const [text, setText] = useState("");
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const pending = useRef(false);
  async function load() {
    setError("");
    try { setStatus(await apiFetch<Status>("/api/integrations")); }
    catch { setError("情報源の接続状態を読み込めませんでした。"); }
  }
  useEffect(() => { void load(); }, []);
  async function submit() {
    if (pending.current) return;
    pending.current = true;
    setBusy(true); setError(""); setMessage("");
    try {
      const result = await apiFetch<{ message: string; created_count: number; duplicate_count: number }>(
        "/api/integrations/conversation", { body: { text, consent } },
      );
      setMessage(result.message);
      setText(""); setConsent(false);
    } catch (error) {
      setError(error instanceof Error ? error.message : "候補を作成できませんでした。");
    } finally { pending.current = false; setBusy(false); }
  }
  return <section className="settings-group" aria-labelledby="source-heading">
    <h2 id="source-heading">情報源から依頼を見つける</h2>
    <Card>
      {error && <p role="alert">{error}</p>}
      {!status ? <Button onClick={() => void load()}>接続状態を再読み込み</Button> : <>
        <p>提案機能：{status.ai.configured ? "設定あり（実接続は未確認）" : "未設定・管理者による設定が必要です"}</p>
        <h3>会話履歴から候補を作る</h3>
        <p>ご自身で選んだ会話だけを貼り付けてください。会話履歴全体を自動取得する機能ではありません。</p>
        <label htmlFor="source-conversation">対象の会話（最大{status.conversation.max_characters.toLocaleString()}文字）</label>
        <textarea id="source-conversation" rows={7} value={text} disabled={busy}
          maxLength={status.conversation.max_characters} style={{ width: "100%" }}
          onChange={event => { setText(event.target.value); setConsent(false); setMessage(""); }} />
        <p><label><input type="checkbox" checked={consent} disabled={busy}
          onChange={event => setConsent(event.target.checked)} />
          この会話を設定済みのAIサービスへ送って候補を作ることに同意する
        </label></p>
        <p>候補の説明と根拠の短い引用を自分の依頼ボードに保存します。タスクになるのは「受ける」を押した後です。</p>
        <Button variant="sun" disabled={busy || !consent || !text.trim() || !status.ai.configured}
          onClick={() => void submit()}>{busy ? "会話から候補を探しています…" : "依頼の候補を作る"}</Button>
        {message && <p role="status">{message} <Link to="/board?tab=requests">依頼ボードへ</Link></p>}
        <p>情報源の認証はSafariなどのブラウザで行ってください。Work連携は任意の追加機能で、直接連携には不要です。</p>
        {new URLSearchParams(window.location.search).get("integration") === "connected" && <p role="status">読み取り用の接続を確認しました。対象範囲を確認してから候補を作成してください。</p>}
        {new URLSearchParams(window.location.search).get("integration") === "failed" && <p role="alert">接続を確認できませんでした。権限と管理者の設定を確認してやり直してください。</p>}
        {new URLSearchParams(window.location.search).get("integration") === "cancelled" && <p role="status">接続をキャンセルしました。</p>}
        <RemoteSource key={`gmail-${status.gmail.account}`} provider="gmail" status={status.gmail} aiConfigured={status.ai.configured} reload={load} />
        <RemoteSource key={`slack-${status.slack.account}-${status.slack.workspace}`} provider="slack" status={status.slack} aiConfigured={status.ai.configured} reload={load} />
      </>}
    </Card>
  </section>;
}
