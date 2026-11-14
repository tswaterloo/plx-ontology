import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  test: {
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    // A ceiling, not a wait: a test ends when it ends. The default 5 s is missed by a heavy
    // first render on a loaded landing host (load avg 30-100, tools/FLAKY.md).
    testTimeout: 30_000,
    hookTimeout: 30_000,
  },
});
