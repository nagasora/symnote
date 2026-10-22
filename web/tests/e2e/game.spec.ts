import { test, expect } from "@playwright/test";
import { resolve } from "node:path";
import { mkdir } from "node:fs/promises";

const headers = { "X-SymNote": "1" };

test("adventure loop stays usable across screens and themes", async ({
  page,
}, info) => {
  const [width, theme] = info.project.name.split("-");
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const login = await page.request.post("/api/auth/e2e-login", {
    headers,
    data: { email: `browser-${width}-${theme}@example.com`, name: "そら" },
  });
  expect(login.ok()).toBeTruthy();
  await page.request.post("/api/auth/onboarding", {
    headers,
    data: { name: "そら" },
  });
  await page.addInitScript(
    (selectedTheme) => localStorage.setItem("symnote_theme", selectedTheme),
    theme,
  );
  await page.goto("/today");
  await expect(
    page.getByRole("heading", { name: "きょうの冒険", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "田中さんへ見積書を送る", exact: true }),
  ).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    )
    .toBe(true);
  await page.evaluate(() => document.fonts.ready);
  const directory = resolve("../docs/redesign/game/verification");
  await mkdir(directory, { recursive: true });
  await page.screenshot({
    path: resolve(directory, `today-${info.project.name}.png`),
    fullPage: true,
  });

  const beforeSplit = await (
    await page.request.get("/api/tasks?status=all")
  ).json();
  await page
    .getByRole("button", { name: "小分けにする", exact: true })
    .first()
    .click();
  const split = page.getByRole("dialog", { name: "小さな一歩を選ぶ" });
  await expect(split).toBeVisible();
  const afterPreview = await (
    await page.request.get("/api/tasks?status=all")
  ).json();
  expect(afterPreview.tasks).toHaveLength(beforeSplit.tasks.length);
  await split.getByRole("button", { name: "あとで考える" }).click();

  await page
    .getByRole("button", { name: "もうクリアした", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "クエストクリア！" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "元に戻す", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "クエストクリア！" }),
  ).not.toBeVisible();
  const afterUndo = await (await page.request.get("/api/player")).json();
  expect(afterUndo.xp_total).toBeGreaterThan(0);

  await page.keyboard.press("Control+k");
  const capture = page.getByRole("dialog", { name: "ミナに渡す" });
  await expect
    .poll(() =>
      capture.evaluate((dialog) => dialog.contains(document.activeElement)),
    )
    .toBe(true);
  await capture
    .getByRole("textbox")
    .first()
    .fill("買い物をする\nメモ：好きな本を書き留める");
  await capture.getByRole("button", { name: "分けて確認する" }).click();
  await capture.getByRole("button", { name: "2つ登録する" }).click();
  await expect(capture).not.toBeVisible();
  await page.goto("/board?tab=requests");
  await expect(
    page.getByText("打ち合わせの候補日を返信する", { exact: true }),
  ).toBeVisible();
  await page
    .locator(".request-card")
    .filter({ has: page.getByRole("heading", { name: "打ち合わせの候補日を返信する", exact: true }) })
    .getByRole("button", { name: "受ける", exact: true })
    .click();
  await expect(
    page.getByText("打ち合わせの候補日を返信する", { exact: true }),
  ).not.toBeVisible();
  await page.route("**/api/candidates/*/approve", (route) =>
    route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify({ error: "検証用の通信エラー" }),
    }),
  );
  await page
    .getByRole("button", { name: "受ける", exact: true })
    .first()
    .click();
  await expect(
    page.getByText("検証用の通信エラー", { exact: false }).first(),
  ).toBeVisible();
  await expect(
    page.getByText("確認用の依頼に返信する", { exact: true }),
  ).toBeVisible();
  await page.unroute("**/api/candidates/*/approve");
  await page
    .getByRole("button", { name: "受ける", exact: true })
    .first()
    .click();

  await page.goto("/map");
  await page
    .getByRole("button", { name: "育てる", exact: true })
    .first()
    .click();
  const grow = page.getByRole("dialog", { name: "たねを育てる" });
  await grow.getByRole("textbox").fill("毎日を楽しくしたい");
  await grow.getByRole("button", { name: "つぎへ" }).click();
  await grow.getByRole("textbox").fill("一冊読み終える");
  await grow.getByRole("button", { name: "つぎへ" }).click();
  await grow.getByRole("button", { name: "地図の案を見る" }).click();
  await grow.getByRole("button", { name: "この地図で冒険をはじめる" }).click();
  await expect(grow).not.toBeVisible();
  const maps = await (await page.request.get("/api/maps")).json();
  expect(maps.maps).toHaveLength(1);
  const tasks = await (await page.request.get("/api/tasks?status=all")).json();
  expect(
    tasks.tasks.some(
      (task: { goal_id: number | null; status: string }) =>
        task.goal_id !== null && task.status === "today",
    ),
  ).toBe(true);

  for (const task of maps.maps[0].stages[0].tasks) {
    await page.request.post(`/api/tasks/${task.id}/complete`, { headers });
  }
  await page.goto("/map");
  await page.locator(".map-card").first().click();
  await page.getByRole("button", { name: "次のステージの地図を描く", exact: true }).click();
  const stage = page.getByRole("dialog", { name: "次のステージの地図を描く" });
  await stage.getByRole("button", { name: "選んだ道を保存" }).click();
  await expect(stage).not.toBeVisible();
  const progressed = await (await page.request.get(`/api/maps/${maps.maps[0].goal_id}`)).json();
  expect(progressed.stages[1].tasks.length).toBeGreaterThan(0);

  await page.goto("/map?view=memos");
  await page.getByPlaceholder("残しておきたいこと").fill("検証用のメモ");
  await page.getByRole("button", { name: "メモを追加", exact: true }).click();
  const memo = page.locator(".memo-library-card").filter({ has: page.getByRole("heading", { name: "検証用のメモ", exact: true }) });
  await memo.getByRole("button", { name: "編集", exact: true }).click();
  await page.locator(".memo-library-list textarea").fill("検証用のメモを編集しました");
  await page.getByRole("button", { name: "保存する", exact: true }).click();
  await expect(page.getByRole("heading", { name: "検証用のメモを編集しました", exact: true })).toBeVisible();

  await page.goto("/map?view=goals");
  await page.getByRole("button", { name: "アーカイブ", exact: true }).click();
  await page.getByRole("button", { name: "進行中に戻す", exact: true }).click();
  expect((await (await page.request.get("/api/maps")).json()).maps).toHaveLength(1);

  const accessibility: unknown[] = [];
  for (const path of ["/today", "/map", "/board", "/me", "/me/settings", "/map?view=calendar", "/map?view=memos", "/map?view=goals", "/map?view=ideas"]) {
    await page.goto(path);
    await page.waitForLoadState("networkidle");
    await expect
      .poll(() =>
        page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      )
      .toBe(true);
    await page.addScriptTag({ url: "/test-assets/axe.js" });
    const violations = await page.evaluate(async () => {
      const result = await (
        window as unknown as {
          axe: {
            run: (
              options: unknown,
            ) => Promise<{
              violations: Array<{
                id: string;
                impact: string;
                nodes: Array<{ html: string }>;
              }>;
            }>;
          };
        }
      ).axe.run({ runOnly: { type: "tag", values: ["wcag2a", "wcag2aa"] } });
      return result.violations
        .filter(
          (item) => item.impact === "serious" || item.impact === "critical",
        )
        .map((item) => ({
          id: item.id,
          html: item.nodes.map((node) => node.html),
        }));
    });
    accessibility.push(
      ...violations.map((violation) => ({ path, ...violation })),
    );
    if (width !== "1024" && (path === "/map" || path === "/me")) {
      await page.screenshot({
        path: resolve(directory, `${path.slice(1)}-${info.project.name}.png`),
        fullPage: true,
      });
    }
  }
  expect(accessibility).toEqual([]);
  await page.route("**/api/player", (route) =>
    route.fulfill({ status: 503, contentType: "application/json", body: "{}" }),
  );
  await page.goto("/today");
  await expect(
    page.getByText("田中さんへ見積書を送る", { exact: true }).first(),
  ).toBeVisible();
  await page.unroute("**/api/player");
  expect(errors).toEqual([]);
});
