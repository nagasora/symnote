import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { apiFetch } from "../../api/client";
import { Button } from "../../components/ui/Button";

export type ConnectionStatus = {
  available: boolean; connected: boolean; reason: string;
  account: string; workspace: string; channels: string[];
  verified_at: number | null; last_fetch_at: number | null;
  last_success_at: number | null; last_error: string;
};
const when = (value: number | null) => value ? new Date(value * 1000).toLocaleString() : "未確認";

export function RemoteSource({ provider, status, aiConfigured, reload }: {
  provider: "gmail" | "slack"; status: ConnectionStatus; aiConfigured: boolean; reload: () => Promise<void>;
}) {
  const name = provider === "gmail" ? "Gmail" : "Slack";
  const [busy, setBusy] = useState(false);
  const [consent, setConsent] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [choices, setChoices] = useState<Array<{ id: string; name: string }>>([]);
  const [cursor, setCursor] = useState("");
  const [selected, setSelected] = useState(status.channels ?? []);
  const lock = useRef(false);
  async function action(task: () => Promise<void>) {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError(""); setMessage("");
    try { await task(); }
    catch (error) { setError(error instanceof Error ? error.message : "連携を処理できませんでした。"); }
    finally { lock.current = false; setBusy(false); }
  }
  const dirty = [...selected].sort().join(",") !== [...(status.channels ?? [])].sort().join(",");
  return <section aria-labelledby={`${provider}-source-title`}>
    <h3 id={`${provider}-source-title`}>{name}</h3>
    <p>{provider === "gmail" ? "メイン受信箱の直近7日間だけを読み取ります。送信・既読化・削除は行いません。" : "本人の読み取り権限で、選択した公開チャンネルの直近7日間だけを読み取ります。Bot追加・チャンネル参加・投稿は行いません。"}</p>
    {error && <p role="alert">{error}</p>}
    {message && <p role="status">{message} <Link to="/board?tab=requests">依頼ボードへ</Link></p>}
    {!status.available && <p>{status.reason}</p>}
    {status.connected ? <>
      <p>接続先：{status.account}{status.workspace ? ` ／ ${status.workspace}` : ""}</p>
      <p>接続確認：{when(status.verified_at)}<br />取得確認：{when(status.last_fetch_at)}<br />取り込み完了：{when(status.last_success_at)}</p>
      {status.last_error && <p>{status.last_error}</p>}
      {provider === "slack" && <>
        <p>対象チャンネルは自動選択しません。最大5つ選べます。変更後は保存してください。</p>
        <p>保存済み：{status.channels?.join(", ") || "未選択"}</p>
        <Button disabled={busy || !status.available} onClick={() => void action(async () => {
          const result = await apiFetch<{ channels: typeof choices; next_cursor: string }>(`/api/integrations/slack/channels${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`);
          setChoices(previous => [...new Map([...previous, ...result.channels].map(c => [c.id, c])).values()]);
          setCursor(result.next_cursor);
        })}>{choices.length && cursor ? "チャンネルをもっと表示" : "公開チャンネルを表示"}</Button>
        <fieldset disabled={busy}><legend>読み取り対象</legend>
          {choices.map(channel => <label key={channel.id} style={{ display: "block", minHeight: 44 }}>
            <input type="checkbox" checked={selected.includes(channel.id)}
              disabled={!selected.includes(channel.id) && selected.length >= 5}
              onChange={event => { setSelected(previous => event.target.checked ? [...previous, channel.id] : previous.filter(id => id !== channel.id)); setConsent(false); }} /> #{channel.name}
          </label>)}
        </fieldset>
        <Button disabled={busy || !dirty || !status.available} onClick={() => void action(async () => {
          await apiFetch("/api/integrations/slack/channels", { method: "PUT", body: { channels: selected } });
          setConsent(false); await reload(); setMessage("読み取り対象を保存しました。");
        })}>対象チャンネルを保存</Button>
      </>}
      <p><label><input type="checkbox" checked={consent} disabled={busy}
        onChange={event => setConsent(event.target.checked)} />
        この範囲の内容を設定済みAIへ送って、承認待ち候補を作ることに同意する
      </label></p>
      <p>Gmailは1回最大20件、Slackは1回1チャンネル・最大15件です。続きは時間をおいて再実行できます。自動的な定期取得は行いません。</p>
      <Button variant="sun" disabled={busy || !consent || !aiConfigured || !status.available || (provider === "slack" && (dirty || !status.channels?.length))}
        onClick={() => void action(async () => {
          try {
            const result = await apiFetch<{ message: string; fetched_count: number; ai_calls: number }>(`/api/integrations/${provider}/import`, { body: { consent } });
            setMessage(`今回の未処理情報：${result.fetched_count}件。AI処理：${result.ai_calls}回。${result.message}`); setConsent(false);
          } finally { await reload(); }
        })}>{busy ? "処理中…" : `${name}から依頼の候補を作る`}</Button>
      <Button disabled={busy} onClick={() => setRemoving(!removing)}>接続を解除する</Button>
      {removing && <div><p>SymNote内の接続情報を削除します。保存済みの候補は残ります。外部サービス側の許可は、そのサービスの管理画面から取り消せます。</p>
        <Button disabled={busy} onClick={() => void action(async () => {
          await apiFetch(`/api/integrations/${provider}`, { method: "DELETE" });
          setChoices([]); setSelected([]); setConsent(false); setRemoving(false); await reload();
        })}>解除を確定する</Button></div>}
    </> : <Button disabled={busy || !status.available} onClick={() => void action(async () => {
      const result = await apiFetch<{ authorization_url: string }>(`/api/integrations/${provider}/start`, { body: {} });
      window.location.assign(result.authorization_url);
    })}>{name}の読み取りを許可する</Button>}
  </section>;
}
