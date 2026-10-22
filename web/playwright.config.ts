import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60000,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: "http://127.0.0.1:8898",
    channel: "chromium",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [390, 1024, 1440].flatMap((width) =>
    ["light", "dark"].map((theme) => ({
      name: `${width}-${theme}`,
      use: {
        viewport: { width, height: width === 390 ? 844 : 900 },
        colorScheme: theme as "light" | "dark",
        isMobile: width === 390,
        hasTouch: width === 390,
      },
    })),
  ),
  webServer: {
    command: "../.venv/bin/python ../scripts/e2e_server.py",
    url: "http://127.0.0.1:8898/api/health",
    reuseExistingServer: false,
    timeout: 30000,
  },
});
