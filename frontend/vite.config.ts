import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The built SPA is served by FastAPI, so it ships inside the Python package.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../src/epub2audiobook/server/static",
    emptyOutDir: true,
  },
  server: {
    // `npm run dev` proxies the API to the Python server on :8000.
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
});
