import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: "./src/test-setup.ts",
    globals: true,
    testTimeout: 20_000,
    exclude: ["**/node_modules/**", "**/dist/**", "**/e2e/**"],
  },
});
