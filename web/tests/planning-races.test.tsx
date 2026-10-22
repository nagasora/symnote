import "@testing-library/jest-dom/vitest";

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const { apiFetchMock, refreshMock, showToastMock } = vi.hoisted(() => ({
  apiFetchMock: vi.fn(),
  refreshMock: vi.fn().mockResolvedValue(undefined),
  showToastMock: vi.fn(),
}));

vi.mock("../src/api/client", () => ({ apiFetch: apiFetchMock }));
vi.mock("../src/features/game/GameContext", () => ({
  useGame: () => ({ refresh: refreshMock, showToast: showToastMock }),
}));

import type { GoalBreakdownPreview } from "../src/api";
import type { SeedItem, SeedPlan } from "../src/api/types";
import { SeedGrow } from "../src/features/map/SeedGrow";
import type { MapStage } from "../src/features/map/MapPath";
import { StageBreakdown } from "../src/features/map/StageBreakdown";

afterEach(() => {
  cleanup();
  apiFetchMock.mockReset();
  refreshMock.mockClear();
  showToastMock.mockClear();
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function seed(id: number, title: string): SeedItem {
  return { id, title, raw_text: title };
}

function plan(prefix: string, estimates: [number, number] = [10, 15]): SeedPlan {
  return {
    stages: [1, 2, 3].map((number) => ({
      title: `${prefix} stage ${number}`,
      why: "",
      target_date: null,
    })),
    first_quests: estimates.map((estimate, index) => ({
      title: `${prefix} quest ${index + 1}`,
      why: "",
      due_hint: "",
      estimate_minutes: estimate,
    })),
    ai_used: true,
    why: "reason",
    done_when: "done",
    target_date: null,
  };
}

function stage(id: number, title: string): MapStage {
  return { id, title, tasks: [] };
}

function breakdown(prefix: string): GoalBreakdownPreview {
  return {
    subgoals: [],
    tasks: [{ title: `${prefix} quest`, why: "", due_hint: "今日", estimate_minutes: 15 }],
    ai_used: true,
  };
}

async function requestSeedPlan() {
  fireEvent.change(screen.getByLabelText("どうしてやりたい？"), {
    target: { value: "理由" },
  });
  fireEvent.click(screen.getByRole("button", { name: /つぎへ/ }));
  fireEvent.change(screen.getByLabelText("できたら、どんな状態？"), {
    target: { value: "完了した状態" },
  });
  fireEvent.click(screen.getByRole("button", { name: /つぎへ/ }));
  fireEvent.click(screen.getByRole("button", { name: /地図の案を見る/ }));
  await waitFor(() => expect(apiFetchMock).toHaveBeenCalled());
}

describe("planning request races", () => {
  it("does not show seed A's delayed plan after switching the sheet to seed B", async () => {
    const requestA = deferred<SeedPlan>();
    apiFetchMock.mockImplementation((path: string) => {
      if (path === "/api/seeds/1/plan") return requestA.promise;
      throw new Error(`unexpected request: ${path}`);
    });

    const props = { onClose: vi.fn(), onAdopted: vi.fn() };
    const view = render(
      <MemoryRouter>
        <SeedGrow seed={seed(1, "Seed A")} {...props} />
      </MemoryRouter>,
    );
    await requestSeedPlan();

    view.rerender(
      <MemoryRouter>
        <SeedGrow seed={seed(2, "Seed B")} {...props} />
      </MemoryRouter>,
    );
    expect(await screen.findByText("Seed B")).toBeInTheDocument();
    expect(screen.getByText("どうしてやりたい？")).toBeInTheDocument();

    await act(async () => requestA.resolve(plan("A")));

    expect(screen.getByText("どうしてやりたい？")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("A stage 1")).not.toBeInTheDocument();
    expect(screen.queryByText("地図の案")).not.toBeInTheDocument();
  });

  it("accepts an AI plan whose first quest is 15 minutes and a later quest is 30 minutes", async () => {
    apiFetchMock.mockImplementation((path: string) => {
      if (path === "/api/seeds/3/plan") return Promise.resolve(plan("valid", [15, 30]));
      throw new Error(`unexpected request: ${path}`);
    });

    render(
      <MemoryRouter>
        <SeedGrow seed={seed(3, "Launch seed")} onClose={vi.fn()} onAdopted={vi.fn()} />
      </MemoryRouter>,
    );
    await requestSeedPlan();

    expect(await screen.findByDisplayValue("valid quest 1")).toBeInTheDocument();
    expect(screen.getByDisplayValue("valid quest 2")).toBeInTheDocument();
    expect(screen.getByLabelText("valid quest 1の見積もり")).toHaveValue("15");
    expect(screen.getByLabelText("valid quest 2の見積もり")).toHaveValue("30");
    expect(screen.queryByText(/先頭は5〜15分/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "この地図で冒険をはじめる" })).toBeEnabled();
  });

  it("ignores stage A's delayed breakdown and applies stage B's response", async () => {
    const requestA = deferred<GoalBreakdownPreview>();
    const requestB = deferred<GoalBreakdownPreview>();
    apiFetchMock.mockImplementation((path: string) => {
      if (path === "/api/goals/11/breakdown") return requestA.promise;
      if (path === "/api/goals/22/breakdown") return requestB.promise;
      throw new Error(`unexpected request: ${path}`);
    });

    const props = { onClose: vi.fn(), onSaved: vi.fn() };
    const view = render(
      <MemoryRouter>
        <StageBreakdown stage={stage(11, "Stage A")} {...props} />
      </MemoryRouter>,
    );
    await waitFor(() => expect(apiFetchMock).toHaveBeenCalledWith("/api/goals/11/breakdown", { body: {} }));

    view.rerender(
      <MemoryRouter>
        <StageBreakdown stage={stage(22, "Stage B")} {...props} />
      </MemoryRouter>,
    );
    await waitFor(() => expect(apiFetchMock).toHaveBeenCalledWith("/api/goals/22/breakdown", { body: {} }));

    await act(async () => requestA.resolve(breakdown("A")));
    expect(screen.queryByDisplayValue("A quest")).not.toBeInTheDocument();
    expect(screen.getByText("ミナが道を考えています…")).toBeInTheDocument();

    await act(async () => requestB.resolve(breakdown("B")));
    expect(await screen.findByDisplayValue("B quest")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("A quest")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "選んだ道を保存" })).toBeEnabled();
  });
});
