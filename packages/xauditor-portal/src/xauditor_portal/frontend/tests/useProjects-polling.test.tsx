import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, useProjects } from "@/lib/api";

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
    },
  });
  const Wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
  return { qc, Wrapper };
}

const PROJECT_RUNNING = {
  project_key: "k-r",
  project_name: "p-r",
  repo_root: "/tmp/r",
  total_graph_builds: 1,
  total_audit_runs: 1,
  running_audit_runs: 1,
  added_at: "2026-05-01T00:00:00Z",
};

const PROJECT_IDLE = {
  project_key: "k-i",
  project_name: "p-i",
  repo_root: "/tmp/i",
  total_graph_builds: 1,
  total_audit_runs: 1,
  running_audit_runs: 0,
  added_at: "2026-05-02T00:00:00Z",
};

describe("useProjects polling", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("refetches every 3s when on the first page (offset: 0)", async () => {
    const spy = vi.spyOn(api, "listProjects").mockResolvedValue({
      items: [PROJECT_IDLE],
      total: 1,
    });
    const { Wrapper } = makeWrapper();

    renderHook(() => useProjects({ limit: 25, offset: 0 }), {
      wrapper: Wrapper,
    });

    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));

    await vi.advanceTimersByTimeAsync(3100);
    await waitFor(() => expect(spy.mock.calls.length).toBeGreaterThanOrEqual(2));

    await vi.advanceTimersByTimeAsync(3100);
    await waitFor(() => expect(spy.mock.calls.length).toBeGreaterThanOrEqual(3));
  });

  it("does not poll on later pages when no row is running", async () => {
    const spy = vi.spyOn(api, "listProjects").mockResolvedValue({
      items: [PROJECT_IDLE],
      total: 50,
    });
    const { Wrapper } = makeWrapper();

    renderHook(() => useProjects({ limit: 25, offset: 25 }), {
      wrapper: Wrapper,
    });

    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));

    // Allow ~7s of fake time to elapse — well past two refetch
    // intervals — and assert the hook stayed at one fetch.
    await vi.advanceTimersByTimeAsync(7000);
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("polls on later pages when at least one visible row reports running", async () => {
    const spy = vi.spyOn(api, "listProjects").mockResolvedValue({
      items: [PROJECT_IDLE, PROJECT_RUNNING],
      total: 50,
    });
    const { Wrapper } = makeWrapper();

    renderHook(() => useProjects({ limit: 25, offset: 25 }), {
      wrapper: Wrapper,
    });

    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));

    await vi.advanceTimersByTimeAsync(3100);
    await waitFor(() => expect(spy.mock.calls.length).toBeGreaterThanOrEqual(2));
  });
});
