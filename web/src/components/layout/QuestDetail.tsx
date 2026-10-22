import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Check, RotateCcw, Scissors, Trash2 } from "lucide-react";
import { apiFetch } from "../../api/client";
import type { GoalItem, TaskItem } from "../../api/types";
import { useGame } from "../../features/game/GameContext";
import { Button } from "../ui/Button";
import { Sheet } from "../ui/Sheet";

type Frequency = "daily" | "weekly" | "weekdays" | "monthly";

export function QuestDetail() {
  const { tasks, refresh, breakdown, complete, reopen, showToast } = useGame();
  const location = useLocation();
  const navigate = useNavigate();
  const id = Number(new URLSearchParams(location.search).get("quest"));
  const [fetched, setFetched] = useState<TaskItem | null>(null);
  const source =
    tasks.find((task) => task.id === id) ??
    (fetched?.id === id ? fetched : null);
  const [draft, setDraft] = useState<TaskItem | null>(null);
  const [goals, setGoals] = useState<GoalItem[]>([]);
  const [frequency, setFrequency] = useState<Frequency | "">("");
  const [interval, setInterval] = useState(1);
  const [weekdays, setWeekdays] = useState<number[]>([]);
  const [monthDay, setMonthDay] = useState(1);
  const [endDate, setEndDate] = useState("");
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    if (id > 0 && !tasks.some((task) => task.id === id)) {
      void apiFetch<{ task: TaskItem }>(`/api/tasks/${id}`)
        .then((result) => {
          if (active) setFetched(result.task);
        })
        .catch(() => {
          if (active) showToast("クエストを読み込めませんでした。");
        });
    }
    return () => {
      active = false;
    };
  }, [id, tasks, showToast]);

  useEffect(() => {
    setDraft(source);
    setDirty(false);
    setError("");
    setFrequency(
      source?.recurrence?.active === false
        ? ""
        : (source?.recurrence?.frequency ?? ""),
    );
    setInterval(source?.recurrence?.interval ?? 1);
    setWeekdays(source?.recurrence?.weekdays ?? []);
    setMonthDay(source?.recurrence?.month_day ?? 1);
    setEndDate(source?.recurrence?.end_date ?? "");
    if (source)
      void apiFetch<{ goals: GoalItem[] }>("/api/goals")
        .then((result) => setGoals(result.goals))
        .catch(() => setError("マップ一覧を読み込めませんでした。"));
  }, [source?.id]);

  function change(patch: Partial<TaskItem>) {
    if (draft) {
      setDraft({ ...draft, ...patch });
      setDirty(true);
    }
  }
  function close() {
    if (saving || (dirty && !window.confirm("変更を保存せずに閉じますか？")))
      return;
    const query = new URLSearchParams(location.search);
    query.delete("quest");
    navigate(`${location.pathname}${query.size ? `?${query}` : ""}`);
  }
  async function save() {
    if (!draft || !draft.title.trim()) return;
    setSaving(true);
    setError("");
    try {
      const result = await apiFetch<{ task: TaskItem }>(
        `/api/tasks/${draft.id}`,
        {
          method: "PATCH",
          body: {
            title: draft.title.trim(),
            details: draft.details,
            due_date: draft.due_date,
            due_time: draft.due_time,
            ...(draft.status !== "done" ? { status: draft.status } : {}),
            importance: draft.importance,
            urgency: draft.urgency,
            effort: draft.effort,
            energy: draft.energy,
            goal_id: draft.goal_id,
            estimate_minutes: draft.estimate_minutes,
            ...(frequency
              ? {
                  recurrence: {
                    frequency,
                    interval,
                    weekdays,
                    month_day: monthDay,
                    end_date: endDate || null,
                  },
                }
              : { stop_recurrence: Boolean(source?.recurrence) }),
          },
        },
      );
      setDraft(result.task);
      setDirty(false);
      await refresh();
      showToast("クエストを保存しました");
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "保存できませんでした。",
      );
    } finally {
      setSaving(false);
    }
  }
  async function remove() {
    if (!draft) return;
    setSaving(true);
    setError("");
    try {
      await apiFetch(`/api/tasks/${draft.id}`, { method: "DELETE" });
      setDirty(false);
      navigate(location.pathname);
      await refresh();
      showToast("ゴミ箱に移しました");
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "ゴミ箱へ移せませんでした。",
      );
    } finally {
      setSaving(false);
    }
  }
  const recurrenceChange = () => setDirty(true);
  return (
    <Sheet
      open={Boolean(source)}
      title="クエストの詳細"
      onClose={close}
      className="quest-detail"
    >
      {draft && (
        <fieldset disabled={saving}>
          <label>
            クエスト名
            <input
              className="detail-title"
              value={draft.title}
              maxLength={200}
              onChange={(event) => change({ title: event.target.value })}
            />
          </label>
          <label>
            手順・メモ
            <textarea
              value={draft.details}
              maxLength={4000}
              onChange={(event) => change({ details: event.target.value })}
            />
          </label>
          <div className="detail-grid">
            <label>
              置き場所
              <select
                value={draft.status}
                onChange={(event) =>
                  change({ status: event.target.value as TaskItem["status"] })
                }
                disabled={source?.status === "done"}
              >
                <option value="today">きょう</option>
                <option value="week">今週</option>
                <option value="inbox">あとで</option>
                {source?.status === "done" && (
                  <option value="done">クリア済み</option>
                )}
              </select>
            </label>
            <label>
              予定日
              <input
                type="date"
                value={draft.due_date ?? ""}
                onChange={(event) =>
                  change({ due_date: event.target.value || null })
                }
              />
            </label>
            <label>
              予定時刻
              <input
                type="time"
                value={draft.due_time ?? ""}
                onChange={(event) =>
                  change({ due_time: event.target.value || null })
                }
              />
            </label>
            <label>
              見積もり（分）
              <input
                type="number"
                min={1}
                max={1440}
                value={draft.estimate_minutes ?? ""}
                onChange={(event) =>
                  change({
                    estimate_minutes: event.target.value
                      ? Number(event.target.value)
                      : null,
                  })
                }
              />
            </label>
            <label>
              重要度
              <select
                value={draft.importance}
                onChange={(event) =>
                  change({ importance: Number(event.target.value) })
                }
              >
                {[1, 2, 3, 4, 5].map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
            <label>
              緊急度
              <select
                value={draft.urgency}
                onChange={(event) =>
                  change({ urgency: Number(event.target.value) })
                }
              >
                {[1, 2, 3, 4, 5].map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
            <label>
              取り組みの長さ
              <select
                value={draft.effort ?? ""}
                onChange={(event) =>
                  change({
                    effort: (event.target.value as TaskItem["effort"]) || null,
                  })
                }
              >
                <option value="">未設定</option>
                <option value="short">短め</option>
                <option value="medium">ふつう</option>
                <option value="long">じっくり</option>
              </select>
            </label>
            <label>
              必要な集中力
              <select
                value={draft.energy ?? ""}
                onChange={(event) =>
                  change({
                    energy: (event.target.value as TaskItem["energy"]) || null,
                  })
                }
              >
                <option value="">未設定</option>
                <option value="low">低め</option>
                <option value="mid">ふつう</option>
                <option value="high">高め</option>
              </select>
            </label>
            <label>
              冒険マップ
              <select
                value={draft.goal_id ?? ""}
                onChange={(event) =>
                  change({
                    goal_id: event.target.value
                      ? Number(event.target.value)
                      : null,
                  })
                }
              >
                <option value="">割り当てなし</option>
                {goals.map((goal) => (
                  <option key={goal.id} value={goal.id}>
                    {"　".repeat(goal.depth)}
                    {goal.title}
                  </option>
                ))}
              </select>
            </label>
            <label>
              繰り返し
              <select
                value={frequency}
                onChange={(event) => {
                  setFrequency(event.target.value as Frequency | "");
                  recurrenceChange();
                }}
              >
                <option value="">なし</option>
                <option value="daily">日ごと</option>
                <option value="weekly">週ごと</option>
                <option value="weekdays">平日</option>
                <option value="monthly">月ごと</option>
              </select>
            </label>
            {frequency && (
              <>
                <label>
                  間隔
                  <input
                    type="number"
                    min={1}
                    max={365}
                    value={interval}
                    onChange={(event) => {
                      setInterval(Number(event.target.value));
                      recurrenceChange();
                    }}
                  />
                </label>
                <label>
                  繰り返しの終了日
                  <input
                    type="date"
                    value={endDate}
                    onChange={(event) => {
                      setEndDate(event.target.value);
                      recurrenceChange();
                    }}
                  />
                </label>
              </>
            )}
            {frequency === "monthly" && (
              <label>
                毎月の日
                <input
                  type="number"
                  min={1}
                  max={31}
                  value={monthDay}
                  onChange={(event) => {
                    setMonthDay(Number(event.target.value));
                    recurrenceChange();
                  }}
                />
              </label>
            )}
            {frequency === "weekly" && (
              <fieldset>
                <legend>曜日</legend>
                {["月", "火", "水", "木", "金", "土", "日"].map(
                  (label, day) => (
                    <label key={day}>
                      <input
                        type="checkbox"
                        checked={weekdays.includes(day)}
                        onChange={(event) => {
                          setWeekdays(
                            event.target.checked
                              ? [...weekdays, day]
                              : weekdays.filter((value) => value !== day),
                          );
                          recurrenceChange();
                        }}
                      />
                      {label}
                    </label>
                  ),
                )}
              </fieldset>
            )}
          </div>
          {draft.source && (
            <p>
              出典：{draft.source.type} {draft.source.excerpt}
              {draft.source.source_url &&
                /^https?:\/\//.test(draft.source.source_url) && (
                  <a
                    href={draft.source.source_url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    原文を開く
                  </a>
                )}
            </p>
          )}
          {error && <p role="alert">{error}</p>}
          <footer>
            <Button
              variant="sun"
              disabled={!dirty || !draft.title.trim()}
              onClick={() => void save()}
            >
              変更を保存
            </Button>
            <Button
              disabled={dirty}
              onClick={() => {
                close();
                void (source?.status === "done"
                  ? reopen(source)
                  : complete(source!));
              }}
            >
              {source?.status === "done" ? (
                <RotateCcw size={17} />
              ) : (
                <Check size={17} />
              )}
              {source?.status === "done" ? "元に戻す" : "クリアする"}
            </Button>
            <Button disabled={dirty} onClick={() => void breakdown(source!)}>
              <Scissors size={17} />
              小分けにする
            </Button>
            <Button variant="quiet" onClick={() => void remove()}>
              <Trash2 size={17} />
              ゴミ箱へ
            </Button>
          </footer>
        </fieldset>
      )}
    </Sheet>
  );
}
