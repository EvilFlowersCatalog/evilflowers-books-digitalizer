/**
 * Vite config for the Restoration Studio SPA.
 *
 * - `/api` is proxied to the FastAPI app in dev so the SPA and the API share an
 *   origin (no CORS, SSE works unchanged).
 * - `--mode mock` loads `.env.mock` (VITE_MOCK=1): the in-browser fixture backend
 *   replaces the network so screens can be built and screenshotted offline.
 * - OpenCV.js (~10 MB) is emitted as a plain asset (`?url`) and injected as a
 *   script on first use; the viewer chunk (OSD + Annotorious/PIXI) is ~0.9 MB,
 *   hence the raised warning limit.
 */
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const API_TARGET = process.env.STUDIO_API_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: API_TARGET, changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    chunkSizeWarningLimit: 1000,
    rollupOptions: {
      output: {
        manualChunks: {
          viewer: ["openseadragon", "@annotorious/openseadragon"],
          react: ["react", "react-dom", "react-router-dom", "@tanstack/react-query"],
        },
      },
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
