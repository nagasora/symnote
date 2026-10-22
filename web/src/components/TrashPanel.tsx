import { useEffect, useState } from "react";
import { apiFetch } from "../api";

interface TrashedItem {
  id: number;
  kind: string;
  title: string;
  deleted_at: string;
}

export function TrashPanel({ onRestored }: { onRestored?: () => void } = {}) {
  const [items, setItems] = useState<TrashedItem[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let active = true;
    apiFetch<{ items: TrashedItem[] }>("/api/trash")
      .then((result) => {
        if (active) setItems(result.items);
      })
      .catch(() => {
        if (active) setError("ゴミ箱を読み込めませんでした。");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  async function restore(id: number) {
    setBusy(true);
    setError("");
    try {
      await apiFetch(`/api/trash/${id}/restore`, { method: "POST" });
      setItems((current) => current.filter((item) => item.id !== id));
      onRestored?.();
    } catch {
      setError("復元できませんでした。再読み込みして確認してください。");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="settings-section" aria-labelledby="trash-heading">
      <h2 id="trash-heading">ゴミ箱</h2>
      <p>
        削除したタスク・メモ・未仕分けを復元できます。自動では消去しません。
      </p>
      <p>
        復元すると新しい項目として戻ります。繰り返し・集中記録・報酬の関連付けは戻りません。
      </p>
      {error && <p role="alert">{error}</p>}
      {loading ? (
        <p role="status">読み込み中…</p>
      ) : items.length === 0 && !error ? (
        <p>ゴミ箱は空です。</p>
      ) : null}
      <ul>
        {items.map((item) => (
          <li key={item.id}>
            <span>
              {item.title}（
              {item.kind === "task"
                ? "タスク"
                : item.kind === "memo"
                  ? "メモ"
                  : "未仕分け"}
              ）
            </span>{" "}
            <time dateTime={item.deleted_at}>
              {item.deleted_at.replace("T", " ")}
            </time>{" "}
            <button
              type="button"
              disabled={busy}
              onClick={() => restore(item.id)}
              aria-label={`${item.title}を復元`}
            >
              復元
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
