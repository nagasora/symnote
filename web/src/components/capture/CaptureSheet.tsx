import { useEffect, useMemo, useRef, useState } from "react";
import { FileText, Mic, Send, Trash2 } from "lucide-react";
import { apiFetch } from "../../api/client";
import type {
  GoalItem,
  QuickPreviewResult,
  SplitDraftItem,
} from "../../api/types";
import { useGame } from "../../features/game/GameContext";
import { Button } from "../ui/Button";
import { Chip } from "../ui/Chip";
import { Sheet } from "../ui/Sheet";

type Draft = SplitDraftItem & { selected: boolean };

export function CaptureSheet() {
  const { captureOpen, setCaptureOpen, refresh, showToast } = useGame();
  const [text, setText] = useState("");
  const [preview, setPreview] = useState<QuickPreviewResult | null>(null);
  const [drafts, setDrafts] = useState<Draft[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [goals, setGoals] = useState<GoalItem[]>([]);
  const previewRequest = useRef(0);
  const splitRequest = useRef(0);
  const submitLock = useRef(false);
  const multiple = useMemo(() => text.trim().split(/\n+/).length > 1, [text]);

  useEffect(() => {
    const request = ++previewRequest.current;
    if (!captureOpen || !text.trim() || multiple) {
      setPreview(null);
      return;
    }
    const id = window.setTimeout(() => {
      void apiFetch<QuickPreviewResult>("/api/capture/preview", {
        body: { text },
      })
        .then((result) => {
          if (previewRequest.current === request) setPreview(result);
        })
        .catch(() => {
          if (previewRequest.current === request) setPreview(null);
        });
    }, 160);
    return () => {
      clearTimeout(id);
      previewRequest.current += 1;
    };
  }, [captureOpen, multiple, text]);

  useEffect(() => {
    if (captureOpen)
      void apiFetch<{ goals: GoalItem[] }>("/api/goals")
        .then((result) => setGoals(result.goals))
        .catch(() => setGoals([]));
  }, [captureOpen]);

  function changeText(value: string) {
    if (submitLock.current) return;
    previewRequest.current += 1;
    splitRequest.current += 1;
    setPreview(null);
    setDrafts(null);
    setText(value);
  }
  function close(force = false) {
    if (submitLock.current && !force) return;
    previewRequest.current += 1;
    splitRequest.current += 1;
    setText("");
    setPreview(null);
    setDrafts(null);
    setBusy(false);
    setCaptureOpen(false);
  }

  async function split() {
    if (submitLock.current) return;
    submitLock.current = true;
    const request = ++splitRequest.current;
    setBusy(true);
    try {
      const result = await apiFetch<{ items: SplitDraftItem[] }>(
        "/api/capture/split",
        { body: { text } },
      );
      if (splitRequest.current === request)
        setDrafts(result.items.map((item) => ({ ...item, selected: true })));
    } catch (error) {
      if (splitRequest.current === request)
        showToast(
          error instanceof Error ? error.message : "分けられませんでした",
        );
    } finally {
      submitLock.current = false;
      if (splitRequest.current === request) setBusy(false);
    }
  }

  async function commit(single = false) {
    if (!text.trim() || submitLock.current) return;
    submitLock.current = true;
    setBusy(true);
    try {
      if (single || !multiple)
        await apiFetch("/api/capture", {
          body: { text, kind: preview?.kind ?? "auto", status: "today" },
        });
      else {
        const items =
          drafts
            ?.filter((item) => item.selected)
            .map(({ selected: _selected, ...item }) => item) ?? [];
        if (!items.length) return;
        await apiFetch("/api/capture/commit-split", {
          body: { source_text: text, save_source_memo: true, items },
        });
      }
      const count =
        single || !multiple
          ? 1
          : (drafts?.filter((item) => item.selected).length ?? 0);
      close(true);
      await refresh();
      showToast(`${count}つ登録しました`);
    } catch (error) {
      showToast(
        error instanceof Error ? error.message : "登録できませんでした",
      );
    } finally {
      submitLock.current = false;
      setBusy(false);
    }
  }

  const SpeechRecognition = (
    window as unknown as {
      webkitSpeechRecognition?: new () => {
        lang: string;
        start: () => void;
        onresult: (event: {
          results: ArrayLike<{ 0: { transcript: string } }>;
        }) => void;
      };
    }
  ).webkitSpeechRecognition;
  return (
    <Sheet
      open={captureOpen}
      title="ミナに渡す"
      onClose={() => close()}
      className="capture-sheet"
    >
      <fieldset className="capture-fields" disabled={busy}>
        <p className="sheet-lead">
          気になっていることを、そのまま置いていってね。
        </p>
        <textarea
          autoFocus
          value={text}
          onChange={(event) => changeText(event.target.value)}
          onKeyDown={(event) => {
            const composing = (
              event as typeof event & { isComposing?: boolean }
            ).isComposing;
            if (
              composing ||
              event.nativeEvent.isComposing ||
              event.keyCode === 229
            )
              return;
            if (event.key === "Enter" && !event.shiftKey && !multiple) {
              event.preventDefault();
              void commit();
            }
          }}
          placeholder="例：あした15時に歯医者を予約する"
          rows={multiple ? 7 : 4}
        />
        {preview && (
          <div className="parsed-chips">
            {preview.chips.map((chip, index) => (
              <Chip
                tone={
                  chip.kind.includes("date") || chip.kind.includes("time")
                    ? "sky"
                    : "plain"
                }
                key={`${chip.kind}-${index}`}
              >
                {chip.label}
              </Chip>
            ))}
          </div>
        )}
        {multiple && !drafts && (
          <div className="capture-hint">
            複数の行があります。クエスト・たね・メモに分けて確認できます。
          </div>
        )}
        {drafts && (
          <div className="split-preview">
            {drafts.map((draft, index) => (
              <div className="split-draft" key={index}>
                <label>
                  <input
                    type="checkbox"
                    checked={draft.selected}
                    onChange={() =>
                      setDrafts(
                        drafts.map((item, itemIndex) =>
                          itemIndex === index
                            ? { ...item, selected: !item.selected }
                            : item,
                        ),
                      )
                    }
                  />
                  <input
                    value={draft.title}
                    onChange={(event) =>
                      setDrafts(
                        drafts.map((item, itemIndex) =>
                          itemIndex === index
                            ? { ...item, title: event.target.value }
                            : item,
                        ),
                      )
                    }
                  />
                  <Button
                    variant="quiet"
                    size="sm"
                    aria-label="この項目を外す"
                    onClick={() =>
                      setDrafts(
                        drafts.filter((_, itemIndex) => itemIndex !== index),
                      )
                    }
                  >
                    <Trash2 size={17} />
                  </Button>
                </label>
                <div>
                  <select
                    aria-label={`${draft.title}の種類`}
                    value={draft.kind ?? "task"}
                    onChange={(event) => {
                      const kind = event.target.value as Draft["kind"];
                      setDrafts(
                        drafts.map((item, itemIndex) =>
                          itemIndex === index
                            ? {
                                ...item,
                                kind,
                                goal_id: kind === "task" ? item.goal_id : null,
                              }
                            : item,
                        ),
                      );
                    }}
                  >
                    <option value="task">クエスト</option>
                    <option value="seed">たね</option>
                    <option value="memo">メモ</option>
                  </select>
                  {(draft.kind ?? "task") === "task" && (
                    <select
                      aria-label={`${draft.title}のマップ`}
                      value={draft.goal_id ?? ""}
                      onChange={(event) =>
                        setDrafts(
                          drafts.map((item, itemIndex) =>
                            itemIndex === index
                              ? {
                                  ...item,
                                  goal_id: event.target.value
                                    ? Number(event.target.value)
                                    : null,
                                }
                              : item,
                          ),
                        )
                      }
                    >
                      <option value="">マップなし</option>
                      {goals.map((goal) => (
                        <option key={goal.id} value={goal.id}>
                          {goal.title}
                        </option>
                      ))}
                    </select>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
        <div className="capture-entry-points">
          <span>
            <FileText size={17} />
            議事録・メールは改行して貼り付け
          </span>
          {SpeechRecognition && (
            <button
              type="button"
              onClick={() => {
                const recognition = new SpeechRecognition();
                recognition.lang = "ja-JP";
                recognition.onresult = (event) => {
                  if (submitLock.current) return;
                  previewRequest.current += 1;
                  splitRequest.current += 1;
                  setPreview(null);
                  setDrafts(null);
                  setText(
                    (value) =>
                      `${value}${value ? " " : ""}${event.results[0][0].transcript}`,
                  );
                };
                recognition.start();
              }}
            >
              <Mic size={17} />
              声で
            </button>
          )}
        </div>
        <footer>
          {multiple && (
            <Button
              variant="quiet"
              onClick={() => void commit(true)}
              disabled={busy}
            >
              まとめて1件
            </Button>
          )}
          <Button
            variant="sun"
            onClick={() => (drafts || !multiple ? void commit() : void split())}
            disabled={busy || !text.trim()}
          >
            <Send size={18} />
            {busy
              ? "準備中…"
              : drafts
                ? `${drafts.filter((item) => item.selected).length}つ登録する`
                : multiple
                  ? "分けて確認する"
                  : "登録する"}
          </Button>
        </footer>
      </fieldset>
    </Sheet>
  );
}
