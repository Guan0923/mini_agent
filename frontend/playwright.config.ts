import { defineConfig } from "@playwright/test";

const frontendPort = process.env.MINI_AGENT_E2E_FRONTEND_PORT ?? "15173";
const backendPort = process.env.MINI_AGENT_E2E_BACKEND_PORT ?? "18080";
const modelPort = process.env.MINI_AGENT_E2E_MODEL_PORT ?? "18081";
const frontendUrl = `http://127.0.0.1:${frontendPort}`;
const backendUrl = `http://127.0.0.1:${backendPort}`;

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  use: {
    baseURL: frontendUrl,
    browserName: "chromium",
    permissions: ["clipboard-read", "clipboard-write"],
    headless: true,
  },
  webServer: [
    {
      command: "uv run --project .. python ../tests/support/turn_e2e_server.py",
      url: `${backendUrl}/api/health`,
      env: {
        MINI_AGENT_E2E_PORT: backendPort,
        MINI_AGENT_E2E_MODEL_PORT: modelPort,
        MINI_AGENT_ALLOWED_ORIGINS: frontendUrl,
        MINI_AGENT_PUBLIC_URL: frontendUrl,
      },
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: `npm run dev -- --host 127.0.0.1 --port ${frontendPort} --strictPort`,
      url: `${frontendUrl}/`,
      env: { MINI_AGENT_BACKEND_URL: backendUrl },
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
