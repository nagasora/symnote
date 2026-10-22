import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/nativeClient", () => ({
  isNativeApp: () => false,
  nativeRequest: vi.fn(),
}));

import { apiFetch, type TaskItem } from "../src/api";
import { QuestRow } from "../src/components/quest/QuestRow";

afterEach(() => {
  cleanup();
});

function task(overrides: Partial<TaskItem> = {}): TaskItem {
  return {
    id: 7,
    title: "発表資料を仕上げる",
    details: "結論のスライドまで作る",
    raw_text: "結論のスライドまで作る",
    status: "today",
    due_date: "2026-10-23",
    due_time: null,
    importance: 4,
    urgency: 3,
    goal_id: 2,
    goal_title: "研究発表",
    estimate_minutes: 90,
    actual_minutes: null,
    completed_at: null,
    recurrence: null,
    source: null,
    created_at: "2026-10-23T09:00:00",
    quest: { xp: 30, gold: 5, stars: 3, is_boss: true },
    ...overrides,
  };
}

function response({
  ok,
  status,
  json,
  text = "",
}: {
  ok: boolean;
  status: number;
  json: unknown | Error;
  text?: string;
}): Response {
  return {
    ok,
    status,
    json: vi
      .fn()
      .mockImplementation(() =>
        json instanceof Error ? Promise.reject(json) : Promise.resolve(json),
      ),
    text: vi.fn().mockResolvedValue(text),
  } as unknown as Response;
}

describe("QuestRow", () => {
  it("uses the local next calendar date for a carryover swipe", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 9, 23, 0, 30));
    try {
      const onChangeDate = vi.fn();
      const { container } = render(
        <QuestRow
          task={task()}
          carryover
          onToggle={vi.fn()}
          onOpen={vi.fn()}
          onChangeDate={onChangeDate}
        />,
      );
      fireEvent.touchStart(container.querySelector("article")!, {
        touches: [{ clientX: 200 }],
      });
      fireEvent.touchEnd(container.querySelector("article")!, {
        changedTouches: [{ clientX: 20 }],
      });
      expect(onChangeDate).toHaveBeenCalledWith("2026-10-24");
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows boss reward and keeps check, details, and breakdown actions separate", () => {
    const onToggle = vi.fn();
    const onOpen = vi.fn();
    const onBreakdown = vi.fn();
    render(
      <QuestRow
        task={task()}
        carryover
        onToggle={onToggle}
        onOpen={onOpen}
        onBreakdown={onBreakdown}
      />,
    );

    expect(screen.getByText("大物 ★★★")).toBeInTheDocument();
    expect(screen.getByText("+30 XP")).toBeInTheDocument();
    expect(screen.getByText("研究発表")).toBeInTheDocument();
    expect(screen.getByText("きのうまでの予定でした")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "クリアにする" }));
    expect(onToggle).toHaveBeenCalledOnce();
    expect(onOpen).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "小分けにする" }));
    expect(onBreakdown).toHaveBeenCalledOnce();
    expect(onOpen).not.toHaveBeenCalled();

    fireEvent.click(screen.getByText("発表資料を仕上げる"));
    expect(onOpen).toHaveBeenCalledOnce();
  });

  it("exposes undo for a cleared quest and hides the pending reward", () => {
    const onToggle = vi.fn();
    const onOpen = vi.fn();
    const { container } = render(
      <QuestRow
        task={task({ status: "done", completed_at: "2026-10-23T10:00:00" })}
        onToggle={onToggle}
        onOpen={onOpen}
      />,
    );

    expect(container.querySelector("article")).toHaveClass("is-done");
    expect(screen.queryByText("+30 XP")).not.toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "クリア済み。元に戻す" }),
    );
    expect(onToggle).toHaveBeenCalledOnce();
    expect(onOpen).not.toHaveBeenCalled();
  });

  it("shows the floating reward while a quest is clearing", () => {
    const { container } = render(
      <QuestRow task={task()} completing onToggle={vi.fn()} onOpen={vi.fn()} />,
    );

    expect(container.querySelector("article")).toHaveClass("just-cleared");
    expect(screen.getAllByText("+30 XP")).toHaveLength(2);
    expect(container.querySelector(".xp-float")).toHaveTextContent("+30 XP");
  });
});

describe("apiFetch browser transport", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("rejects a successful HTML or truncated response instead of returning empty state", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        response({
          ok: true,
          status: 200,
          json: new SyntaxError("invalid JSON"),
          text: "private proxy details",
        }),
      ),
    );
    await expect(apiFetch("/api/today")).rejects.toThrow(
      "サーバーからの応答を読み取れませんでした",
    );
  });

  it("sends JSON mutations with the CSRF header and same-origin browser credentials", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(response({ ok: true, status: 200, json: { id: 9 } }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      apiFetch<{ id: number }>("/api/tasks", {
        method: "POST",
        body: { title: "小さな一歩" },
      }),
    ).resolves.toEqual({ id: 9 });
    expect(fetchMock).toHaveBeenCalledOnce();

    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = init.headers as Record<string, string>;
    expect(path).toBe("/api/tasks");
    expect(init.credentials).toBe("same-origin");
    expect(headers["X-SymNote"]).toBe("1");
    expect(headers["Content-Type"]).toBe("application/json");
    expect(headers).not.toHaveProperty("Authorization");
    expect(headers).not.toHaveProperty("Cookie");
    expect(init.body).toBe(JSON.stringify({ title: "小さな一歩" }));
  });

  it("uses a status-only error when an error response is not valid JSON", async () => {
    const leakedText = "passcode=should-not-appear";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        response({
          ok: false,
          status: 502,
          json: new SyntaxError("invalid JSON"),
          text: leakedText,
        }),
      ),
    );

    const error = await apiFetch("/api/tasks").catch(
      (reason: unknown) => reason,
    );
    expect(error).toBeInstanceOf(Error);
    expect((error as Error).message).toBe("通信エラーが発生しました (502)");
    expect((error as Error).message).not.toContain(leakedText);
  });

  it("surfaces a structured API error without exposing browser credentials", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        response({
          ok: false,
          status: 409,
          json: { error: "変更が競合しました" },
        }),
      ),
    );

    await expect(apiFetch("/api/tasks/7")).rejects.toThrow(
      "変更が競合しました",
    );
  });
});
