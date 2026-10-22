import { useEffect, useRef, useState } from "react";
import { apiFetch } from "../../api/client";
import type { GoalBreakdownPreview } from "../../api";
import { useGame } from "../game/GameContext";
import { Button } from "../../components/ui/Button";
import { Sheet } from "../../components/ui/Sheet";
import type { MapStage } from "./MapPath";

export function StageBreakdown({
  stage,
  onClose,
  onSaved,
}: {
  stage: MapStage | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { showToast } = useGame();
  const [preview, setPreview] = useState<GoalBreakdownPreview | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState(false);
  const requestVersion = useRef(0);
  const saving = useRef(false);

  function reset() {
    setPreview(null);
    setSelected(new Set());
  }
  function close(force = false) {
    if (saving.current && !force) return;
    requestVersion.current += 1;
    reset();
    setBusy(false);
    onClose();
  }

  useEffect(() => {
    const version = ++requestVersion.current;
    reset();
    if (!stage) {
      setBusy(false);
      return;
    }
    setBusy(true);
    void apiFetch<GoalBreakdownPreview>(`/api/goals/${stage.id}/breakdown`, {
      body: {},
    })
      .then((value) => {
        if (requestVersion.current !== version) return;
        setPreview(value);
        setSelected(new Set(value.tasks.map((_, index) => index)));
      })
      .catch((error) => {
        if (requestVersion.current !== version) return;
        showToast(
          error instanceof Error ? error.message : "次の地図を描けませんでした",
        );
        close(true);
      })
      .finally(() => {
        if (requestVersion.current === version) setBusy(false);
      });
    return () => {
      if (requestVersion.current === version) requestVersion.current += 1;
    };
  }, [stage?.id, showToast]);

  async function save() {
    if (!stage || !preview || !selected.size || saving.current) return;
    saving.current = true;
    const version = ++requestVersion.current;
    const stageId = stage.id;
    setBusy(true);
    try {
      await apiFetch(`/api/goals/${stageId}/breakdown/commit`, {
        body: {
          subgoals: preview.subgoals,
          tasks: preview.tasks,
          selected_subgoals: [],
          selected_tasks: [...selected].sort((a, b) => a - b),
        },
      });
      if (requestVersion.current !== version) return;
      onSaved();
      close(true);
      showToast("次のステージの地図を描きました");
    } catch (error) {
      if (requestVersion.current === version)
        showToast(
          error instanceof Error ? error.message : "保存できませんでした",
        );
    } finally {
      saving.current = false;
      if (requestVersion.current === version) setBusy(false);
    }
  }

  return (
    <Sheet
      open={Boolean(stage)}
      title="次のステージの地図を描く"
      onClose={() => close()}
    >
      <fieldset className="capture-fields" disabled={busy}>
        <p>{stage?.title}で進むクエストを選んでください。</p>
        {busy && !preview ? (
          <p>ミナが道を考えています…</p>
        ) : (
          <div className="breakdown-preview">
            {preview?.tasks.map((task, index) => (
              <label key={index}>
                <input
                  type="checkbox"
                  checked={selected.has(index)}
                  onChange={() =>
                    setSelected((current) => {
                      const next = new Set(current);
                      if (next.has(index)) next.delete(index);
                      else next.add(index);
                      return next;
                    })
                  }
                />
                <input
                  value={task.title}
                  onChange={(event) =>
                    setPreview({
                      ...preview,
                      tasks: preview.tasks.map((item, itemIndex) =>
                        itemIndex === index
                          ? { ...item, title: event.target.value }
                          : item,
                      ),
                    })
                  }
                />
                <select
                  value={task.estimate_minutes ?? 15}
                  onChange={(event) =>
                    setPreview({
                      ...preview,
                      tasks: preview.tasks.map((item, itemIndex) =>
                        itemIndex === index
                          ? {
                              ...item,
                              estimate_minutes: Number(event.target.value),
                            }
                          : item,
                      ),
                    })
                  }
                >
                  <option value="5">5分</option>
                  <option value="15">15分</option>
                  <option value="25">25分</option>
                  <option value="45">45分</option>
                </select>
              </label>
            ))}
          </div>
        )}
        <footer>
          <Button onClick={() => close()}>キャンセル</Button>
          <Button
            variant="sun"
            disabled={busy || !selected.size}
            onClick={() => void save()}
          >
            選んだ道を保存
          </Button>
        </footer>
      </fieldset>
    </Sheet>
  );
}
