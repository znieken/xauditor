import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderOptions } from "@testing-library/react";
import { ThemeProvider } from "next-themes";
import * as React from "react";

function createTestQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });
}

export function renderWithProviders(
  ui: React.ReactElement,
  options: (Omit<RenderOptions, "wrapper"> & { theme?: "light" | "dark" | "system" }) = {},
) {
  const { theme = "system", ...rest } = options;
  const queryClient = createTestQueryClient();
  const Wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider attribute="class" defaultTheme={theme} enableSystem>
        {children}
      </ThemeProvider>
    </QueryClientProvider>
  );
  return {
    queryClient,
    ...render(ui, { wrapper: Wrapper, ...rest }),
  };
}
