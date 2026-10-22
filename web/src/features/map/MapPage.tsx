import { useEffect, useState } from "react";
import { ListFilter, Map as MapIcon, Network, Sprout } from "lucide-react";
import { useSearchParams } from "react-router-dom";
import { apiFetch } from "../../api/client";
import type { GoalBreakdownPreview } from "../../api";
import type { MapSummary, SeedItem, TaskItem } from "../../api/types";
import { useGame } from "../game/GameContext";
import { QuestList } from "../../components/quest/QuestList";
import { Button } from "../../components/ui/Button";
import { Card } from "../../components/ui/Card";
import { Chip } from "../../components/ui/Chip";
import { EmptyState } from "../../components/ui/EmptyState";
import { ProgressBar } from "../../components/ui/ProgressBar";
import { Segmented } from "../../components/ui/Segmented";
import { CalendarView } from "./CalendarView";
import { IdeasAdvanced } from "./IdeasAdvanced";
import { AdvancedGoals } from "./AdvancedGoals";
import { MemoLibrary } from "./MemoLibrary";
import { MapPath, type MapStage } from "./MapPath";
import { SeedGrow } from "./SeedGrow";
import { StageBreakdown } from "./StageBreakdown";

type Mode = "maps" | "all" | "calendar" | "ideas" | "goals" | "memos";
interface MapDetail extends MapSummary {
  stages: MapStage[];
  progress: number;
  done_count?: number;
  total_count?: number;
}

export function MapPage() {
  const [params, setParams] = useSearchParams();
  const mode = (params.get("view") as Mode) || "maps";
  const {
    tasks,
    complete,
    breakdown,
    openQuest,
    refresh,
    showToast,
    completingId,
  } = useGame();
  const [maps, setMaps] = useState<MapSummary[]>([]);
  const [seeds, setSeeds] = useState<SeedItem[]>([]);
  const [detail, setDetail] = useState<MapDetail | null>(null);
  const [growing, setGrowing] = useState<SeedItem | null>(null);
  const [drawing, setDrawing] = useState<MapStage | null>(null);
  const [search, setSearch] = useState("");
  const [seedText, setSeedText] = useState("");

  const load = async () => {
    const [mapResult, seedResult] = await Promise.all([
      apiFetch<{ maps?: MapSummary[] } | MapSummary[]>("/api/maps"),
      apiFetch<{ seeds?: SeedItem[] } | SeedItem[]>("/api/seeds"),
    ]);
    setMaps(Array.isArray(mapResult) ? mapResult : (mapResult.maps ?? []));
    setSeeds(Array.isArray(seedResult) ? seedResult : (seedResult.seeds ?? []));
  };
  useEffect(() => {
    void load();
  }, []);
  async function openMap(map: MapSummary) {
    setDetail(await apiFetch<MapDetail>(`/api/maps/${map.goal_id ?? map.id}`));
  }
  async function reloadDetail() {
    if (detail)
      setDetail(
        await apiFetch<MapDetail>(`/api/maps/${detail.goal_id ?? detail.id}`),
      );
    await load();
  }
  async function addToday(task: TaskItem) {
    await apiFetch(`/api/tasks/${task.id}`, {
      method: "PATCH",
      body: { status: "today" },
    });
    await refresh();
    await reloadDetail();
    showToast("きょうの冒険に入れました");
  }
  async function addSeed() {
    if (!seedText.trim()) return;
    await apiFetch("/api/seeds", { body: { text: seedText } });
    setSeedText("");
    await load();
  }
  async function makeFiveMinuteStep(map: MapSummary) {
    const goalId = map.goal_id ?? map.id;
    try {
      const preview = await apiFetch<GoalBreakdownPreview>(
        `/api/goals/${goalId}/breakdown`,
        { body: { count: 1, estimate_minutes: 5 } },
      );
      if (!preview.tasks.length)
        return showToast("いま作れる一歩が見つかりませんでした");
      preview.tasks[0].estimate_minutes = 5;
      await apiFetch(`/api/goals/${goalId}/breakdown/commit`, {
        body: {
          subgoals: preview.subgoals,
          tasks: preview.tasks,
          selected_subgoals: [],
          selected_tasks: [0],
        },
      });
      await Promise.all([load(), refresh()]);
      showToast("5分でできる一歩を作りました");
    } catch (error) {
      showToast(
        error instanceof Error ? error.message : "一歩を作れませんでした",
      );
    }
  }
  const filtered = tasks.filter((task) =>
    `${task.title} ${task.details}`
      .toLowerCase()
      .includes(search.toLowerCase()),
  );
  return (
    <section className="game-screen map-screen">
      <header className="game-page-head">
        <div>
          <span className="eyebrow">
            <MapIcon size={16} />
            GO ON AN ADVENTURE
          </span>
          <h1>冒険マップ</h1>
        </div>
      </header>
      <Segmented
        value={mode === "goals" || mode === "memos" ? "maps" : mode}
        onChange={(value) => setParams(value === "maps" ? {} : { view: value })}
        label="マップの表示"
        options={[
          { value: "maps", label: "マップ" },
          { value: "all", label: "ぜんぶ" },
          { value: "calendar", label: "予定表" },
          { value: "ideas", label: "ひろげる" },
        ]}
      />
      {mode === "maps" && (
        <nav className="advanced-map-links" aria-label="詳しい管理">
          <Button size="sm" onClick={() => setParams({ view: "goals" })}>
            <Network size={16} />
            マップを詳しく管理
          </Button>
          <Button size="sm" onClick={() => setParams({ view: "memos" })}>
            メモを管理
          </Button>
        </nav>
      )}
      {(mode === "goals" || mode === "memos") && (
        <Button
          className="advanced-back"
          variant="quiet"
          onClick={() => setParams({})}
        >
          冒険マップへ戻る
        </Button>
      )}
      {mode === "maps" && (
        <div className="map-grid">
          <div className="map-column">
            {detail ? (
              <>
                <Button variant="quiet" onClick={() => setDetail(null)}>
                  マップ一覧へ
                </Button>
                <Card className="map-detail-head">
                  <Chip tone="grape">冒険マップ</Chip>
                  <h2>{detail.title}</h2>
                  <div className="map-progress">
                    <ProgressBar
                      tone="sky"
                      value={
                        detail.progress_percent ??
                        Math.round(detail.progress * 100)
                      }
                    />
                    <span className="num">
                      {detail.progress_percent ??
                        Math.round(detail.progress * 100)}
                      %
                    </span>
                  </div>
                </Card>
                <MapPath
                  stages={detail.stages ?? []}
                  onAddToday={(task) => void addToday(task)}
                  onOpen={openQuest}
                  onDrawStage={setDrawing}
                />
              </>
            ) : maps.length ? (
              maps.map((map) => (
                <Card className="map-card-wrap" key={map.id ?? map.goal_id}>
                  <button
                    type="button"
                    className="map-card"
                    onClick={() => void openMap(map)}
                  >
                    <span className="map-flag">
                      <MapIcon />
                    </span>
                    <div>
                      <Chip tone="grape">冒険マップ</Chip>
                      <h2>{map.title}</h2>
                      <p>
                        {map.current_stage ?? map.current_stage_number ?? 1} /{" "}
                        {map.stage_count ?? 1} ステージ
                      </p>
                      <ProgressBar
                        tone="sky"
                        value={
                          map.progress_percent ??
                          Math.round((map.progress ?? 0) * 100)
                        }
                      />
                    </div>
                    {map.stale_days ? (
                      <Chip tone="coin">{map.stale_days}日 ひと休み中</Chip>
                    ) : null}
                  </button>
                  {map.stale_days ? (
                    <div className="stale-nudge">
                      <p>5分でできる一歩を出してみる？</p>
                      <Button
                        size="sm"
                        variant="sun"
                        onClick={() => void makeFiveMinuteStep(map)}
                      >
                        5分の一歩を作る
                      </Button>
                    </div>
                  ) : null}
                </Card>
              ))
            ) : (
              <EmptyState title="まだ冒険マップがありません">
                たねを育てると、進む道がここに現れます。
              </EmptyState>
            )}
          </div>
          <aside className="seed-shelf">
            <header>
              <div>
                <Sprout />
                <h2>たね</h2>
              </div>
              <span>{seeds.length}つ</span>
            </header>
            <form
              onSubmit={(event) => {
                event.preventDefault();
                void addSeed();
              }}
            >
              <input
                value={seedText}
                onChange={(event) => setSeedText(event.target.value)}
                placeholder="育てたいことを置く"
              />
              <Button variant="sun" size="sm">
                たねにする
              </Button>
            </form>
            {seeds.map((seed) => (
              <Card key={seed.id} className="seed-card">
                <Chip tone="grape">たね</Chip>
                <h3>{seed.title}</h3>
                <p>{seed.body || seed.raw_text}</p>
                <Button
                  variant="sun"
                  size="sm"
                  onClick={() => setGrowing(seed)}
                >
                  育てる
                </Button>
              </Card>
            ))}
          </aside>
        </div>
      )}
      {mode === "all" && (
        <div className="all-quests">
          <label className="search-box">
            <ListFilter />
            <input
              type="search"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="クエストを検索"
            />
          </label>
          <QuestList
            tasks={filtered}
            completingId={completingId}
            onToggle={complete}
            onOpen={openQuest}
            onBreakdown={breakdown}
          />
        </div>
      )}
      {mode === "calendar" && <CalendarView tasks={tasks} onOpen={openQuest} />}
      {mode === "ideas" && <IdeasAdvanced />}
      {mode === "goals" && <AdvancedGoals onChanged={load} />}
      {mode === "memos" && <MemoLibrary onChanged={load} />}
      <SeedGrow
        seed={growing}
        onClose={() => setGrowing(null)}
        onAdopted={() => void load()}
      />
      <StageBreakdown
        stage={drawing}
        onClose={() => setDrawing(null)}
        onSaved={() => void reloadDetail()}
      />
    </section>
  );
}
