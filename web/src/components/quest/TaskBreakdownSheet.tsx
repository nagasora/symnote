import { useState } from "react";
import { apiFetch } from "../../api/client";
import { useGame, type BreakdownDraft } from "../../features/game/GameContext";
import { Button } from "../ui/Button";
import { Sheet } from "../ui/Sheet";

export function TaskBreakdownSheet() {
  const { breakdownPreview, setBreakdownPreview, refresh, showToast } =
    useGame();
  const [saving, setSaving] = useState(false);
  const close = () => {
    if (!saving) setBreakdownPreview(null);
  };
  const update = (index: number, patch: Partial<BreakdownDraft>) => {
    if (!breakdownPreview) return;
    setBreakdownPreview({
      ...breakdownPreview,
      drafts: breakdownPreview.drafts.map((draft, position) =>
        position === index ? { ...draft, ...patch } : draft,
      ),
    });
  };
  const chosen =
    breakdownPreview?.drafts.filter((draft) => draft.selected) ?? [];
  const valid =
    chosen.length > 0 &&
    chosen.every(
      (draft) =>
        draft.title.trim() &&
        draft.title.length <= 200 &&
        draft.estimate_minutes >= 1 &&
        draft.estimate_minutes <= 1440,
    );
  async function save() {
    if (!breakdownPreview || !valid) return;
    setSaving(true);
    try {
      await apiFetch("/api/capture/commit-split", {
        body: {
          items: chosen.map((draft) => ({
            title: draft.title.trim(),
            estimate_minutes: draft.estimate_minutes,
            goal_id: breakdownPreview.task.goal_id,
            status: "today",
            kind: "task",
          })),
        },
      });
      setBreakdownPreview(null);
      await refresh();
      showToast(`${chosen.length}つの小さな一歩をきょうに入れました`);
    } catch (error) {
      showToast(
        error instanceof Error
          ? error.message
          : "小さな一歩を保存できませんでした",
      );
    } finally {
      setSaving(false);
    }
  }
  return (
    <Sheet
      open={Boolean(breakdownPreview)}
      title="小さな一歩を選ぶ"
      onClose={close}
    >
      <p>
        「{breakdownPreview?.task.title}
        」から、できそうな一歩を選んでね。元のクエストも残ります。
      </p>
      <div className="plan-editor">
        {breakdownPreview?.drafts.map((draft, index) => (
          <fieldset key={index} disabled={saving}>
            <legend>一歩 {index + 1}</legend>
            <label>
              <input
                type="checkbox"
                checked={draft.selected}
                onChange={(event) =>
                  update(index, { selected: event.target.checked })
                }
              />
              きょうに入れる
            </label>
            <label>
              やること
              <input
                value={draft.title}
                maxLength={200}
                onChange={(event) =>
                  update(index, { title: event.target.value })
                }
              />
            </label>
            <label>
              目安（分）
              <input
                type="number"
                min={1}
                max={1440}
                value={draft.estimate_minutes}
                onChange={(event) =>
                  update(index, {
                    estimate_minutes: Number(event.target.value),
                  })
                }
              />
            </label>
          </fieldset>
        ))}
      </div>
      <footer>
        <Button onClick={close} disabled={saving}>
          あとで考える
        </Button>
        <Button
          variant="sun"
          disabled={saving || !valid}
          onClick={() => void save()}
        >
          {saving ? "保存しています…" : `${chosen.length}つをきょうに入れる`}
        </Button>
      </footer>
    </Sheet>
  );
}
