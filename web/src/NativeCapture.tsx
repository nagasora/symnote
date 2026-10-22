import { useEffect, useState } from "react";
import { Share } from "@capacitor/share";
import { readDeviceDraft, saveDeviceDraft } from "./nativeDrafts";

export function NativeCapture() {
  const [text, setText] = useState("");
  const [savedText, setSavedText] = useState("");
  const [savedAt, setSavedAt] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    readDeviceDraft().then((draft) => {
      if (!active) return;
      setText(draft?.text ?? "");
      setSavedText(draft?.text ?? "");
      setSavedAt(draft?.savedAt ?? "");
      setLoading(false);
    }).catch(() => {
      if (!active) return;
      setError("端末内のメモを読み込めませんでした。再起動してお試しください。");
      // Keep the editor locked so a failed read cannot overwrite a saved draft.
    });
    return () => { active = false; };
  }, []);

  async function save() {
    setSaving(true);
    setError("");
    try {
      const draft = await saveDeviceDraft(text);
      setSavedText(draft.text);
      setSavedAt(draft.savedAt);
    } catch {
      setError("保存できませんでした。入力内容はこの画面に残っています。");
    } finally {
      setSaving(false);
    }
  }

  return <main className="auth-wrap" style={{ paddingTop: "max(24px, env(safe-area-inset-top))", paddingBottom: "max(24px, env(safe-area-inset-bottom))" }}>
    <section className="card auth-card" style={{ width: "100%", maxWidth: 640 }}>
      <div className="brand"><span className="brand-mark">S</span><span className="brand-name">SymNote</span></div>
      <h1 className="serif">この端末のメモ</h1>
      <p>思いついたことを、ネットにつながっていなくても書き留められます。</p>
      <label htmlFor="device-draft">メモ</label>
      <textarea id="device-draft" value={text} disabled={loading || saving} onChange={(event) => setText(event.target.value)} rows={12} placeholder="今、頭にあることを書いてみましょう" style={{ width: "100%", boxSizing: "border-box", font: "inherit", padding: 12 }} />
      <p role="status" aria-live="polite">{savedAt ? (savedText === text ? `端末に保存済み · ${new Date(savedAt).toLocaleString("ja-JP")}` : "未保存の変更があります") : "保存すると、この端末で読み返せます。"}</p>
      {error && <p role="alert">{error}</p>}
      <div style={{ display: "flex", gap: 12 }}>
        <button className="btn primary" disabled={loading || saving || savedText === text} onClick={save}>{saving ? "保存中…" : "端末に保存"}</button>
        <button className="btn" disabled={loading || saving || !text.trim()} onClick={() => {
          Share.share({ title: "SymNoteのメモ", text }).catch(() => {});
        }}>共有する</button>
      </div>
      <p style={{ color: "var(--ink-2)", fontSize: 13 }}>このメモは端末内専用で、自動送信されません。クラウドのタスク・メモ・ゴールは「クラウド」から開けます。アプリを削除すると端末内のデータも失われるため、大切な内容は「共有する」から控えを残してください。</p>
    </section>
  </main>;
}
