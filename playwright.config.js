import { defineConfig } from "@playwright/test";

const origin = "http://127.0.0.1:8177";

export default defineConfig({
  testDir: "./frontend/tests",
  timeout: 30_000,
  workers: 3,
  reporter: "list",
  use: { baseURL: origin, trace: "retain-on-failure" },
  projects: ["chromium", "firefox", "webkit"].map((browserName) => ({
    name: browserName,
    use: { browserName },
  })),
  webServer: {
    command: "python -m uvicorn app.main:app --host 127.0.0.1 --port 8177 --no-proxy-headers",
    env: { APP_ENV: "development", DB_PASSWORD: "design-fixtures-only" },
    url: `${origin}/dev/design`,
    reuseExistingServer: false,
  },
});
