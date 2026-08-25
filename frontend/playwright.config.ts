import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  use: {
    baseURL: "http://127.0.0.1:15173",
    browserName: "chromium",
    permissions: ["clipboard-read", "clipboard-write"],
    headless: true,
  },
  webServer: [
    {
      command: "python ../tests/support/turn_e2e_server.py",
      url: "http://127.0.0.1:18080/api/health",
      env: {
        MINI_AGENT_E2E_PORT: "18080",
        MINI_AGENT_ALLOWED_ORIGINS: "http://127.0.0.1:15173",
        MINI_AGENT_PUBLIC_URL: "http://127.0.0.1:15173",
      },
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: "npm run dev -- --host 127.0.0.1 --port 15173 --strictPort",
      url: "http://127.0.0.1:15173/",
      env: { MINI_AGENT_BACKEND_URL: "http://127.0.0.1:18080" },
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
