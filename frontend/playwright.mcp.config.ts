import { defineConfig } from "@playwright/test";

const frontendPort = process.env.MINI_AGENT_E2E_FRONTEND_PORT ?? "15273";
const backendPort = process.env.MINI_AGENT_E2E_PORT ?? "18280";
const modelPort = process.env.MINI_AGENT_E2E_MODEL_PORT ?? "18281";
const mcpPort = process.env.MINI_AGENT_E2E_MCP_PORT ?? "18282";
const frontendUrl = `http://127.0.0.1:${frontendPort}`;
const env = {
  MINI_AGENT_E2E_PORT: backendPort,
  MINI_AGENT_E2E_MODEL_PORT: modelPort,
  MINI_AGENT_ALLOWED_ORIGINS: frontendUrl,
  MINI_AGENT_PUBLIC_URL: frontendUrl,
  MINI_AGENT_REDIS_KEY_PREFIX: `mini-agent:e2e:mcp:${process.pid}:${Date.now()}`,
};

export default defineConfig({
  testDir: "./e2e",
  testMatch: "mcp-capabilities.spec.ts",
  timeout: 120000,
  workers: 1,
  use: { baseURL: frontendUrl, headless: true, actionTimeout: 15000 },
  webServer: [
    {
      command: `uv run --project .. python ../tests/support/mcp_capabilities_server.py --port ${mcpPort}`,
      url: `http://127.0.0.1:${mcpPort}/health`,
      reuseExistingServer: false,
    },
    {
      command: "uv run --project .. python ../tests/support/mcp_browser_backend.py",
      env,
      url: `http://127.0.0.1:${backendPort}/api/health`,
      reuseExistingServer: false,
      timeout: 120000,
    },
    {
      command: `npm run dev -- --host 127.0.0.1 --port ${frontendPort} --strictPort`,
      env: { MINI_AGENT_BACKEND_URL: `http://127.0.0.1:${backendPort}` },
      url: frontendUrl,
      reuseExistingServer: false,
    },
  ],
});
