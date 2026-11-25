/// <reference types="vitest/config" />
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

import { securityHeaders } from "./csp";

// The FastAPI server the dev server forwards /api to (HTTP and the
// station event WebSockets); override with API_PROXY_TARGET.
const apiTarget = process.env.API_PROXY_TARGET ?? "http://127.0.0.1:8000";

// The production build, as `vite preview` serves it, gets the same security
// headers production must send (see csp.ts). The dev server does not: Vite's
// dev client injects inline scripts the policy would block.
const indexHtml = readFileSync(resolve(__dirname, "index.html"), "utf8");

export default defineConfig({
  plugins: [react()],
  preview: {
    headers: securityHeaders(indexHtml),
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: apiTarget,
        changeOrigin: true,
        ws: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["src/test/setup.ts"],
    restoreMocks: true,
    unstubGlobals: true,
  },
});
