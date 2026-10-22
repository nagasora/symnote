import { useMemo, useState } from "react";
import { CalendarDays, ChevronLeft, ChevronRight } from "lucide-react";
import type { TaskItem } from "../../api/types";
import { Button } from "../../components/ui/Button";
import { Card } from "../../components/ui/Card";

function dayKey(day: Date) {
  return `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`;
}

export function CalendarView({
  tasks,
  onOpen,
}: {
  tasks: TaskItem[];
  onOpen: (task: TaskItem) => void;
}) {
  const now = new Date();
  const todayKey = dayKey(now);
  const [month, setMonth] = useState(
    () => new Date(now.getFullYear(), now.getMonth(), 1),
  );
  const [selectedDay, setSelectedDay] = useState(todayKey);
  const cells = useMemo(() => {
    const first = new Date(month);
    first.setDate(1 - ((first.getDay() + 6) % 7));
    return Array.from({ length: 42 }, (_, index) => {
      const day = new Date(first);
      day.setDate(first.getDate() + index);
      return day;
    });
  }, [month]);
  const byDay = useMemo(
    () =>
      tasks.reduce<Record<string, TaskItem[]>>((groups, task) => {
        if (task.due_date && task.status !== "done")
          (groups[task.due_date] ??= []).push(task);
        return groups;
      }, {}),
    [tasks],
  );
  const move = (offset: number) =>
    setMonth(new Date(month.getFullYear(), month.getMonth() + offset, 1));
  const goToday = () => {
    setMonth(new Date(now.getFullYear(), now.getMonth(), 1));
    setSelectedDay(todayKey);
  };
  const selectedTasks = byDay[selectedDay] ?? [];
  const selectedDate = new Date(`${selectedDay}T00:00:00`);

  return (
    <div className="calendar-view">
      <div className="month-calendar">
        <header>
          <Button variant="quiet" aria-label="前の月" onClick={() => move(-1)}>
            <ChevronLeft />
          </Button>
          <h2>
            {month.toLocaleDateString("ja-JP", {
              year: "numeric",
              month: "long",
            })}
          </h2>
          <Button variant="quiet" aria-label="次の月" onClick={() => move(1)}>
            <ChevronRight />
          </Button>
          <Button size="sm" className="calendar-today" onClick={goToday}>
            今月
          </Button>
        </header>
        <div className="weekday-row">
          {"月火水木金土日".split("").map((day) => (
            <span key={day}>{day}</span>
          ))}
        </div>
        <div className="month-grid">
          {cells.map((day) => {
            const key = dayKey(day);
            const inMonth = day.getMonth() === month.getMonth();
            const dayTasks = byDay[key] ?? [];
            return (
              <div
                className={`calendar-day ${inMonth ? "" : "outside"} ${key === todayKey ? "today" : ""} ${key === selectedDay ? "selected" : ""}`}
                key={key}
              >
                <button
                  type="button"
                  className="calendar-date-select"
                  aria-label={`${day.toLocaleDateString("ja-JP")}、クエスト${dayTasks.length}つ`}
                  aria-pressed={key === selectedDay}
                  onClick={() => setSelectedDay(key)}
                >
                  <span className="num">{day.getDate()}</span>
                  {dayTasks.length > 0 && (
                    <i aria-hidden="true">{dayTasks.length}</i>
                  )}
                </button>
                {dayTasks.slice(0, 3).map((task) => (
                  <button
                    type="button"
                    className="calendar-task"
                    key={task.id}
                    onClick={() => onOpen(task)}
                  >
                    {task.due_time && <small>{task.due_time}</small>}
                    {task.title}
                  </button>
                ))}
                {dayTasks.length > 3 && (
                  <small>ほか {dayTasks.length - 3}つ</small>
                )}
              </div>
            );
          })}
        </div>
      </div>
      <Card className="calendar-day-detail">
        <header>
          <CalendarDays />
          <div>
            <h3>
              {selectedDate.toLocaleDateString("ja-JP", {
                month: "long",
                day: "numeric",
                weekday: "short",
              })}
            </h3>
            <span>{selectedTasks.length}つのクエスト</span>
          </div>
        </header>
        {selectedTasks.length ? (
          selectedTasks.map((task) => (
            <button type="button" key={task.id} onClick={() => onOpen(task)}>
              <span className="num">{task.due_time || "予定"}</span>
              <strong>{task.title}</strong>
            </button>
          ))
        ) : (
          <p>この日のクエストはありません。</p>
        )}
      </Card>
    </div>
  );
}
