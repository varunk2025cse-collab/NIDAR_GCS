import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The backend holds live MAVLink links and runs as a single process on the
// ground station. In development we proxy to it so the browser sees one origin
// and no CORS or cookie weirdness; in production this bundle is served as
// static files from the same host and the proxy is irrelevant.
const BACKEND = process.env.VITE_BACKEND_ORIGIN ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: BACKEND, changeOrigin: true },
      "/ws": { target: BACKEND, ws: true, changeOrigin: true },
      // Locally cached basemap tiles, when served by the backend or a
      // sidecar static server. See scripts/cache-tiles.mjs.
      "/tiles": { target: BACKEND, changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
    // Everything must be bundled. A mission must never depend on a CDN.
    assetsInlineLimit: 4096,
    sourcemap: true,
  },
});
