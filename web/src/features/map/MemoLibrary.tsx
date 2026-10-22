import { useCallback, useEffect, useState } from "react";
import { FileText, Plus, Search, Sprout, Trash2 } from "lucide-react";
import { apiFetch } from "../../api/client";
import type { MemoItem } from "../../api/types";
import { useGame } from "../game/GameContext";
import { Button } from "../../components/ui/Button";
import { Card } from "../../components/ui/Card";
import { Chip } from "../../components/ui/Chip";
import { EmptyState } from "../../components/ui/EmptyState";

type MemoPayload = Omit<MemoItem, "raw_text" | "tags"> & {
  raw_text?: string;
  text?: string;
  tags?: string | string[];
  tags_text?: string;
};

export function normalizeMemo(memo: MemoPayload): MemoItem {
  return {
    ...memo,
    raw_text: memo.raw_text ?? memo.text ?? memo.body ?? "",
    tags:
      typeof memo.tags === "string"
        ? memo.tags
        : (memo.tags_text ?? memo.tags?.join(" ") ?? ""),
  };
}

export function MemoLibrary({ onChanged }: { onChanged: () => Promise<void> }) {
  const { refresh, showToast } = useGame();
  const [memos, setMemos] = useState<MemoItem[]>([]);
  const [query, setQuery] = useState("");
  const [newText, setNewText] = useState("");
  const [editing, setEditing] = useState<MemoItem | null>(null);
  const [busy, setBusy] = useState<number | "new" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    setError(null);
    try {
      setMemos(
        (
          await apiFetch<{ memos: MemoPayload[] }>(
            `/api/memos${query.trim() ? `?q=${encodeURIComponent(query.trim())}` : ""}`,
          )
        ).memos.map(normalizeMemo),
      );
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "メモを読み込めませんでした",
      );
    }
  }, [query]);
  useEffect(() => {
    const id = window.setTimeout(() => void load(), 180);
    return () => clearTimeout(id);
  }, [load]);

  async function run(id: number | "new", action: () => Promise<void>) {
    if (busy) return;
    setBusy(id);
    setError(null);
    try {
      await action();
    } catch (reason) {
      const message =
        reason instanceof Error ? reason.message : "メモを更新できませんでした";
      setError(message);
      showToast(message);
    } finally {
      setBusy(null);
    }
  }
  async function create() {
    if (!newText.trim()) return;
    await run("new", async () => {
      await apiFetch("/api/memos", { body: { text: newText.trim() } });
      setNewText("");
      await load();
      showToast("メモを追加しました");
    });
  }
  async function save() {
    if (!editing?.raw_text.trim()) return;
    await run(editing.id, async () => {
      await apiFetch(`/api/memos/${editing.id}`, {
        method: "PATCH",
        body: { text: editing.raw_text, tags: editing.tags },
      });
      setEditing(null);
      await load();
      showToast("メモを保存しました");
    });
  }
  async function remove(memo: MemoItem) {
    await run(memo.id, async () => {
      await apiFetch(`/api/memos/${memo.id}`, { method: "DELETE" });
      setEditing(null);
      await load();
      showToast("メモをゴミ箱に移しました");
    });
  }
  async function convert(memo: MemoItem, kind: "task" | "seed") {
    await run(memo.id, async () => {
      await apiFetch(
        `/api/memos/${memo.id}/${kind === "task" ? "to-task" : "to-seed"}`,
        { body: {} },
      );
      setEditing(null);
      await Promise.all([load(), refresh(), onChanged()]);
      showToast(
        kind === "task" ? "きょうのクエストにしました" : "たねに移しました",
      );
    });
  }

  return (
    <section className="advanced-library">
      <header>
        <div>
          <FileText />
          <h2>メモの管理</h2>
        </div>
        <label className="search-box">
          <Search />
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="メモを検索"
          />
        </label>
      </header>
      <Card className="memo-create">
        <textarea
          value={newText}
          onChange={(event) => setNewText(event.target.value)}
          placeholder="残しておきたいこと"
        />
        <Button
          variant="sun"
          disabled={busy !== null || !newText.trim()}
          onClick={() => void create()}
        >
          <Plus size={17} />
          メモを追加
        </Button>
      </Card>
      {error && (
        <Card className="board-error" role="alert">
          <p>{error}</p>
          <Button size="sm" onClick={() => void load()}>
            もう一度試す
          </Button>
        </Card>
      )}
      <div className="memo-library-list">
        {memos.length ? (
          memos.map((memo) => (
            <Card className="memo-library-card" key={memo.id}>
              {editing?.id === memo.id ? (
                <>
                  <textarea
                    value={editing.raw_text}
                    onChange={(event) =>
                      setEditing({ ...editing, raw_text: event.target.value })
                    }
                  />
                  <label>
                    タグ
                    <input
                      value={editing.tags}
                      onChange={(event) =>
                        setEditing({ ...editing, tags: event.target.value })
                      }
                    />
                  </label>
                </>
              ) : (
                <>
                  <div>
                    <Chip>メモ</Chip>
                    {memo.tags && <Chip tone="sky">{memo.tags}</Chip>}
                  </div>
                  <h3>{memo.title}</h3>
                  <p>{memo.body || memo.raw_text}</p>
                </>
              )}
              <footer>
                {editing?.id === memo.id ? (
                  <>
                    <Button
                      disabled={busy !== null}
                      onClick={() => setEditing(null)}
                    >
                      取り消す
                    </Button>
                    <Button
                      variant="sun"
                      disabled={busy !== null || !editing.raw_text.trim()}
                      onClick={() => void save()}
                    >
                      保存する
                    </Button>
                  </>
                ) : (
                  <>
                    <Button
                      disabled={busy !== null}
                      onClick={() => setEditing({ ...memo })}
                    >
                      編集
                    </Button>
                    <Button
                      disabled={busy !== null}
                      onClick={() => void convert(memo, "task")}
                    >
                      クエストにする
                    </Button>
                    <Button
                      disabled={busy !== null}
                      onClick={() => void convert(memo, "seed")}
                    >
                      <Sprout size={17} />
                      たねにする
                    </Button>
                    <Button
                      variant="quiet"
                      disabled={busy !== null}
                      aria-label={`「${memo.title}」をゴミ箱に移す`}
                      onClick={() => void remove(memo)}
                    >
                      <Trash2 size={17} />
                    </Button>
                  </>
                )}
              </footer>
            </Card>
          ))
        ) : (
          <EmptyState title="メモはありません">
            残したい文章を追加できます。
          </EmptyState>
        )}
      </div>
    </section>
  );
}
