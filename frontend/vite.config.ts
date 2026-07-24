import vue from "@vitejs/plugin-vue";
import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";

const DEFAULT_BACKEND_ORIGIN = "http://127.0.0.1:8000";

function resolveBackendOrigin(value: string | undefined): string {
  const candidate = value?.trim() || DEFAULT_BACKEND_ORIGIN;
  const parsed = new URL(candidate);
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new Error(
      "MATERIALSAGENT_BACKEND_ORIGIN must use http or https.",
    );
  }
  return parsed.origin;
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const backendOrigin = resolveBackendOrigin(
    env.MATERIALSAGENT_BACKEND_ORIGIN,
  );

  return {
    plugins: [vue()],
    server: {
      host: "127.0.0.1",
      proxy: {
        "/api": {
          target: backendOrigin,
        },
      },
    },
    test: {
      environment: "jsdom",
      setupFiles: "./src/test/setup.ts",
      clearMocks: true,
      restoreMocks: true,
    },
  };
});
