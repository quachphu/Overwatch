import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `base: "./"` keeps asset URLs relative so the built `dist/` can be served both at `/`
// (FastAPI reads dist/index.html directly) and mounted at `/app` as static files.
// See app/main.py.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      // So `npm run dev` can post to the real FastAPI ingress without CORS.
      "/api": "http://localhost:8000",
      "/evidence": "http://localhost:8000",
    },
  },
});
