import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { viteStaticCopy } from "vite-plugin-static-copy";
import { resolve } from "node:path";

const backendUrl = process.env.MINI_AGENT_BACKEND_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  resolve: {
    // markdown-it-texmath has a CommonJS fallback require("katex") even when
    // a custom engine is supplied. Keep that dead fallback out of the bundle.
    alias: {
      katex: resolve(__dirname, "src/katex-shim.ts"),
    },
  },
  plugins: [
    react(),
    viteStaticCopy({
      targets: [
        {
          src: "node_modules/mathjax/**/*",
          dest: "mathjax",
        },
      ],
    }),
  ],
  test: {
    environment: "jsdom",
    setupFiles: "./src/test-setup.ts",
    globals: true,
  },
  server: {
    proxy: {
      "/api": { target: backendUrl, ws: true },
      "/benchmark": backendUrl,
    },
  },
});
