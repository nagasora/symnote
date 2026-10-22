import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowLeft, ArrowRight, Plus, Trash2 } from "lucide-react";
import { apiFetch } from "../../api/client";
import type { SeedItem, SeedPlan } from "../../api/types";
import { useGame } from "../game/GameContext";
import { Button } from "../../components/ui/Button";
import { Sheet } from "../../components/ui/Sheet";

export function SeedGrow({
  seed,
  onClose,
  onAdopted,
}: {
  seed: SeedItem | null;
  onClose: () => void;
  onAdopted: () => void;
}) {
  const navigate = useNavigate();
  const { refresh, showToast } = useGame();
  const [step, setStep] = useState(1);
  const [why, setWhy] = useState("");
  const [doneWhen, setDoneWhen] = useState("");
  const [targetDate, setTargetDate] = useState("");
  const [plan, setPlan] = useState<SeedPlan | null>(null);
  const [busy, setBusy] = useState(false);
  const [todayFirst, setTodayFirst] = useState(true);
  const requestVersion = useRef(0);
  const currentSeed = useRef(seed?.id);
  const adopting = useRef(false);
  if (currentSeed.current !== seed?.id) {
    currentSeed.current = seed?.id;
    requestVersion.current += 1;
  }
  useEffect(() => {
    setStep(1);
    setWhy("");
    setDoneWhen("");
    setTargetDate("");
    setPlan(null);
    setBusy(false);
    setTodayFirst(true);
  }, [seed?.id]);
  const validPlan = Boolean(
    plan &&
    plan.stages.length >= 3 &&
    plan.stages.length <= 5 &&
    plan.first_quests.length >= 2 &&
    plan.first_quests.length <= 4 &&
    plan.stages.every((stage) => stage.title.trim()) &&
    plan.first_quests.every(
      (quest, index) =>
        quest.title.trim() &&
        (quest.estimate_minutes ?? 15) >= 5 &&
        (quest.estimate_minutes ?? 15) <= (index === 0 ? 15 : 60),
    ),
  );

  const close = () => {
    if (adopting.current) return;
    requestVersion.current += 1;
    setStep(1);
    setPlan(null);
    setBusy(false);
    onClose();
  };
  async function buildPlan() {
    if (!seed || busy) return;
    const version = ++requestVersion.current;
    setBusy(true);
    try {
      const result = await apiFetch<SeedPlan>(`/api/seeds/${seed.id}/plan`, {
        body: { why, done_when: doneWhen, target_date: targetDate || null },
      });
      if (version === requestVersion.current) {
        setPlan(result);
        setStep(4);
      }
    } catch (error) {
      if (version === requestVersion.current)
        showToast(
          error instanceof Error ? error.message : "地図を描けませんでした",
        );
    } finally {
      if (version === requestVersion.current) setBusy(false);
    }
  }
  async function adopt() {
    if (!seed || !plan || !validPlan || adopting.current) return;
    adopting.current = true;
    setBusy(true);
    try {
      await apiFetch(`/api/seeds/${seed.id}/adopt`, {
        body: { plan, today_first: todayFirst },
      });
      await refresh();
      onAdopted();
      adopting.current = false;
      close();
      showToast("たねから冒険マップを作りました");
    } catch (error) {
      showToast(
        error instanceof Error ? error.message : "地図を始められませんでした",
      );
    } finally {
      adopting.current = false;
      setBusy(false);
    }
  }
  return (
    <Sheet
      open={Boolean(seed)}
      title="たねを育てる"
      onClose={close}
      className="seed-grow"
    >
      {seed && (
        <fieldset disabled={busy}>
          <div className="seed-title">
            <span>たね</span>
            <h3>{seed.title}</h3>
          </div>
          <Button
            variant="quiet"
            onClick={() => {
              const seedId = seed.id;
              close();
              navigate(`/map?view=ideas&seed=${seedId}`);
            }}
          >
            ミナとひろげる
          </Button>
          {step < 4 ? (
            <>
              <div className="step-dots">
                <span className={step >= 1 ? "on" : ""}>1</span>
                <i />
                <span className={step >= 2 ? "on" : ""}>2</span>
                <i />
                <span className={step >= 3 ? "on" : ""}>3</span>
              </div>
              {step === 1 && (
                <label>
                  <strong>どうしてやりたい？</strong>
                  <textarea
                    value={why}
                    onChange={(event) => setWhy(event.target.value)}
                    placeholder="大切にしたい理由をひとことで"
                    autoFocus
                  />
                </label>
              )}
              {step === 2 && (
                <label>
                  <strong>できたら、どんな状態？</strong>
                  <textarea
                    value={doneWhen}
                    onChange={(event) => setDoneWhen(event.target.value)}
                    placeholder="終わりがわかる景色を書こう"
                    autoFocus
                  />
                </label>
              )}
              {step === 3 && (
                <label>
                  <strong>いつごろまで？</strong>
                  <input
                    type="date"
                    value={targetDate}
                    onChange={(event) => setTargetDate(event.target.value)}
                    autoFocus
                  />
                  <span className="field-help">決めなくても大丈夫です。</span>
                </label>
              )}
              <footer>
                {step > 1 && (
                  <Button onClick={() => setStep(step - 1)}>
                    <ArrowLeft size={17} />
                    戻る
                  </Button>
                )}
                <Button
                  variant="sun"
                  disabled={
                    step === 1
                      ? !why.trim()
                      : step === 2
                        ? !doneWhen.trim()
                        : busy
                  }
                  onClick={() =>
                    step < 3 ? setStep(step + 1) : void buildPlan()
                  }
                >
                  {busy
                    ? "描いています…"
                    : step === 3
                      ? "地図の案を見る"
                      : "つぎへ"}
                  <ArrowRight size={17} />
                </Button>
              </footer>
            </>
          ) : (
            plan && (
              <div className="plan-editor">
                <header>
                  <span>地図の案</span>
                  <h3>{seed.title}</h3>
                </header>
                <div className="stage-editor">
                  {plan.stages.map((stage, index) => (
                    <label key={index}>
                      <span className="stage-number num">{index + 1}</span>
                      <input
                        value={stage.title}
                        onChange={(event) =>
                          setPlan({
                            ...plan,
                            stages: plan.stages.map((item, itemIndex) =>
                              itemIndex === index
                                ? { ...item, title: event.target.value }
                                : item,
                            ),
                          })
                        }
                      />
                      <Button
                        variant="quiet"
                        size="sm"
                        aria-label="ステージを外す"
                        disabled={plan.stages.length <= 3}
                        onClick={() =>
                          setPlan({
                            ...plan,
                            stages: plan.stages.filter(
                              (_, itemIndex) => itemIndex !== index,
                            ),
                          })
                        }
                      >
                        <Trash2 size={16} />
                      </Button>
                    </label>
                  ))}
                  <Button
                    variant="quiet"
                    size="sm"
                    disabled={plan.stages.length >= 5}
                    onClick={() =>
                      setPlan({
                        ...plan,
                        stages: [
                          ...plan.stages,
                          {
                            title: "新しいステージ",
                            why: "",
                            target_date: null,
                          },
                        ],
                      })
                    }
                  >
                    <Plus size={16} />
                    ステージを足す
                  </Button>
                </div>
                <div className="first-quests">
                  <strong>最初のクエスト（2〜4つ）</strong>
                  {plan.first_quests.map((quest, index) => (
                    <label key={index}>
                      <input
                        type="checkbox"
                        checked
                        onChange={() =>
                          plan.first_quests.length > 2 &&
                          setPlan({
                            ...plan,
                            first_quests: plan.first_quests.filter(
                              (_, itemIndex) => itemIndex !== index,
                            ),
                          })
                        }
                      />
                      <input
                        value={quest.title}
                        onChange={(event) =>
                          setPlan({
                            ...plan,
                            first_quests: plan.first_quests.map(
                              (item, itemIndex) =>
                                itemIndex === index
                                  ? { ...item, title: event.target.value }
                                  : item,
                            ),
                          })
                        }
                      />
                      <select
                        aria-label={`${quest.title}の見積もり`}
                        value={quest.estimate_minutes ?? 15}
                        onChange={(event) =>
                          setPlan({
                            ...plan,
                            first_quests: plan.first_quests.map(
                              (item, itemIndex) =>
                                itemIndex === index
                                  ? {
                                      ...item,
                                      estimate_minutes: Number(
                                        event.target.value,
                                      ),
                                    }
                                  : item,
                            ),
                          })
                        }
                      >
                        {Array.from(
                          new Set([
                            5,
                            10,
                            15,
                            25,
                            30,
                            45,
                            60,
                            quest.estimate_minutes ?? 15,
                          ]),
                        )
                          .filter(
                            (minutes) => minutes <= (index === 0 ? 15 : 60),
                          )
                          .sort((a, b) => a - b)
                          .map((minutes) => (
                            <option value={minutes} key={minutes}>
                              {minutes}分
                            </option>
                          ))}
                      </select>
                    </label>
                  ))}
                  <Button
                    variant="quiet"
                    size="sm"
                    disabled={plan.first_quests.length >= 4}
                    onClick={() =>
                      setPlan({
                        ...plan,
                        first_quests: [
                          ...plan.first_quests,
                          {
                            title: "新しいクエスト",
                            why: "",
                            due_hint: "",
                            estimate_minutes: 15,
                          },
                        ],
                      })
                    }
                  >
                    <Plus size={16} />
                    クエストを足す
                  </Button>
                </div>
                <label className="today-first">
                  <input
                    type="checkbox"
                    checked={todayFirst}
                    onChange={(event) => setTodayFirst(event.target.checked)}
                  />
                  最初の1つをきょうに入れる
                </label>
                {!validPlan && (
                  <p className="plan-error">
                    ステージは3〜5つ、最初のクエストは2〜4つ。先頭は5〜15分、残りは5〜60分で整えてください。
                  </p>
                )}
                <footer>
                  <Button onClick={() => setStep(3)}>質問に戻る</Button>
                  <Button
                    variant="sun"
                    disabled={busy || !validPlan}
                    onClick={() => void adopt()}
                  >
                    {busy ? "はじめています…" : "この地図で冒険をはじめる"}
                  </Button>
                </footer>
              </div>
            )
          )}
        </fieldset>
      )}
    </Sheet>
  );
}
