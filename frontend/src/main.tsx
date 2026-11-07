/**
 * Entry point. Mock mode (VITE_MOCK=1) is installed *before* the first render and
 * through a dynamic import, so the fixture backend never ships in a normal build.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles/global.css";

async function boot(): Promise<void> {
  if (import.meta.env.VITE_MOCK === "1") {
    const { installMock } = await import("./mock/install");
    installMock();
  }
  const client = new QueryClient({
    defaultOptions: {
      queries: { staleTime: 30_000, refetchOnWindowFocus: false, retry: 1 },
    },
  });
  const root = document.getElementById("root");
  if (!root) throw new Error("#root missing");
  createRoot(root).render(
    <StrictMode>
      <QueryClientProvider client={client}>
        <App />
      </QueryClientProvider>
    </StrictMode>,
  );
}

void boot();
