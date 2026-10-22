import { Check, Flag, LockKeyhole, MapPinned, Star } from "lucide-react";
import { Button } from "../../components/ui/Button";
import type { TaskItem } from "../../api/types";

export interface MapStage {
  id: number;
  title: string;
  status?: string;
  state?: string;
  is_current?: boolean;
  tasks: Array<TaskItem & { state?: string; is_current?: boolean }>;
}

export function MapPath({
  stages,
  onAddToday,
  onOpen,
  onDrawStage,
}: {
  stages: MapStage[];
  onAddToday: (task: TaskItem) => void;
  onOpen: (task: TaskItem) => void;
  onDrawStage: (stage: MapStage) => void;
}) {
  return (
    <div className="map-path">
      {stages.map((stage, stageIndex) => (
        <section className="map-stage" key={stage.id}>
          <header>
            <span className="stage-number num">{stageIndex + 1}</span>
            <div>
              <small>ステージ {stageIndex + 1}</small>
              <h3>{stage.title}</h3>
            </div>
          </header>
          <div className="path-nodes">
            {stage.tasks.map((task, index) => {
              const state =
                task.state ??
                (task.status === "done"
                  ? "cleared"
                  : task.is_current
                    ? "current"
                    : "upcoming");
              return (
                <div
                  className={`path-node ${state} ${index % 2 ? "offset" : ""}`}
                  key={task.id}
                >
                  <button
                    type="button"
                    className="node-dot"
                    onClick={() => onOpen(task)}
                    aria-label={`${task.title}を開く`}
                  >
                    {state === "cleared" ? (
                      <Check />
                    ) : state === "current" ? (
                      <Star />
                    ) : (
                      <LockKeyhole />
                    )}
                  </button>
                  <div className="node-copy">
                    <strong>{task.title}</strong>
                    {task.estimate_minutes && (
                      <span>{task.estimate_minutes}分</span>
                    )}
                    {state === "current" && task.status !== "today" && (
                      <Button
                        size="sm"
                        variant="sun"
                        onClick={() => onAddToday(task)}
                      >
                        きょうに入れる
                      </Button>
                    )}
                  </div>
                </div>
              );
            })}
            {stage.tasks.length === 0 &&
              (stage.is_current || stage.state === "current") && (
                <div className="draw-stage">
                  <MapPinned />
                  <p>ここから先の道は、進むときに描こう。</p>
                  <Button variant="sun" onClick={() => onDrawStage(stage)}>
                    次のステージの地図を描く
                  </Button>
                </div>
              )}
            <div className="path-node goal">
              <span className="node-dot">
                <Flag />
              </span>
              <strong>ステージクリア</strong>
            </div>
          </div>
        </section>
      ))}
    </div>
  );
}
