import type { TaskItem } from "../../api";
import { QuestRow } from "./QuestRow";

export function QuestList({
  tasks,
  carryover,
  completingId,
  onToggle,
  onOpen,
  onBreakdown,
  onMoveToday,
  onChangeDate,
}: {
  tasks: TaskItem[];
  carryover?: boolean;
  completingId?: number | null;
  onToggle: (task: TaskItem) => void;
  onOpen: (task: TaskItem) => void;
  onBreakdown?: (task: TaskItem) => void;
  onMoveToday?: (task: TaskItem) => void;
  onChangeDate?: (task: TaskItem, day: string) => void;
}) {
  return (
    <div className="quest-list">
      {tasks.map((task) => (
        <QuestRow
          key={task.id}
          task={task}
          carryover={carryover}
          completing={completingId === task.id}
          onToggle={() => onToggle(task)}
          onOpen={() => onOpen(task)}
          onBreakdown={onBreakdown ? () => onBreakdown(task) : undefined}
          onMoveToday={onMoveToday ? () => onMoveToday(task) : undefined}
          onChangeDate={
            onChangeDate ? (day) => onChangeDate(task, day) : undefined
          }
        />
      ))}
    </div>
  );
}
