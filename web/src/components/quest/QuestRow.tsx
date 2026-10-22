import { useRef } from "react";
import { CalendarDays, Scissors } from "lucide-react";
import type { TaskItem } from "../../api";
import { Chip } from "../ui/Chip";
import { CheckButton } from "./CheckButton";

export function QuestRow({
  task,
  carryover = false,
  completing = false,
  onToggle,
  onOpen,
  onBreakdown,
  onMoveToday,
  onChangeDate,
}: {
  task: TaskItem;
  carryover?: boolean;
  completing?: boolean;
  onToggle: () => void;
  onOpen: () => void;
  onBreakdown?: () => void;
  onMoveToday?: () => void;
  onChangeDate?: (day: string) => void;
}) {
  const touchX = useRef<number | null>(null);
  const quest = task.quest ?? {
    xp: task.estimate_minutes && task.estimate_minutes > 60 ? 30 : 10,
    gold: 0,
    stars:
      task.estimate_minutes && task.estimate_minutes > 60
        ? 3
        : task.estimate_minutes && task.estimate_minutes > 15
          ? 2
          : 1,
    is_boss: Boolean(task.estimate_minutes && task.estimate_minutes > 60),
  };
  const done = task.status === "done";
  return (
    <article
      className={`quest-row ${done ? "is-done" : ""} ${completing ? "just-cleared" : ""}`}
      onClick={onOpen}
      onTouchStart={(event) => {
        touchX.current = event.touches[0].clientX;
      }}
      onTouchEnd={(event) => {
        if (
          !carryover ||
          touchX.current == null ||
          touchX.current - event.changedTouches[0].clientX < 70
        )
          return;
        const day = new Date();
        day.setDate(day.getDate() + 1);
        onChangeDate?.(
          `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`,
        );
        touchX.current = null;
      }}
    >
      <CheckButton done={done} boss={quest.is_boss} onClick={onToggle} />
      <div className="quest-copy">
        <strong>{task.title}</strong>
        <div className="quest-chips">
          {quest.is_boss ? (
            <Chip tone="berry">大物 ★★★</Chip>
          ) : (
            <Chip>
              {"★".repeat(quest.stars)}
              {task.estimate_minutes ? ` ${task.estimate_minutes}分` : ""}
            </Chip>
          )}
          {(task.due_date || task.due_time) && (
            <Chip tone="sky">
              <CalendarDays size={14} />
              {task.due_time || task.due_date}
            </Chip>
          )}
          {task.goal_title && <Chip tone="grape">{task.goal_title}</Chip>}
          {task.source && <Chip>{task.source.type} から</Chip>}
          {carryover && (
            <span className="carryover-note">きのうまでの予定でした</span>
          )}
        </div>
        {carryover && (
          <div className="carryover-actions">
            <button
              type="button"
              onClick={(event) => {
                event.stopPropagation();
                onMoveToday?.();
              }}
            >
              きょうやる
            </button>
            <label onClick={(event) => event.stopPropagation()}>
              日を変える
              <input
                type="date"
                aria-label={`${task.title}の日を変える`}
                onChange={(event) =>
                  event.target.value && onChangeDate?.(event.target.value)
                }
              />
            </label>
          </div>
        )}
        {quest.is_boss && onBreakdown && (
          <button
            type="button"
            className="inline-action"
            onClick={(event) => {
              event.stopPropagation();
              onBreakdown();
            }}
          >
            <Scissors size={15} />
            小分けにする
          </button>
        )}
      </div>
      {!done && (
        <Chip tone="xp" className="quest-xp num">
          +{quest.xp} XP
        </Chip>
      )}
      {completing && <span className="xp-float num">+{quest.xp} XP</span>}
    </article>
  );
}
