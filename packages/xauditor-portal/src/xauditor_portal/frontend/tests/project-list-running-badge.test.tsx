import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, cleanup } from "@testing-library/react";
import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ProjectListPage from "@/app/(authenticated)/reports/page";

const useProjectsMock = vi.fn();

vi.mock("@/lib/api", () => ({
  useProjects: (...args: unknown[]) => useProjectsMock(...args),
}));

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const RUNNING_PROJECT = {
  project_key: "k-running",
  project_name: "running-project",
  repo_root: "/tmp/running",
  total_graph_builds: 1,
  total_audit_runs: 5,
  running_audit_runs: 2,
  added_at: "2026-05-01T00:00:00Z",
};

const IDLE_PROJECT = {
  project_key: "k-idle",
  project_name: "idle-project",
  repo_root: "/tmp/idle",
  total_graph_builds: 1,
  total_audit_runs: 3,
  running_audit_runs: 0,
  added_at: "2026-05-02T00:00:00Z",
};

describe("Project list running-audits badge", () => {
  beforeEach(() => {
    useProjectsMock.mockReset();
  });
  afterEach(() => {
    cleanup();
  });

  it("renders the animated badge for projects with running audits and omits it for idle ones", () => {
    useProjectsMock.mockReturnValue({
      data: { items: [RUNNING_PROJECT, IDLE_PROJECT], total: 2 },
      isLoading: false,
      error: null,
    });
    wrap(<ProjectListPage />);

    const runningBadge = screen.getByRole("status", {
      name: /2 audits running for running-project/i,
    });
    expect(runningBadge).toBeInTheDocument();
    expect(runningBadge.textContent).toBe("2");
    expect(runningBadge.querySelector(".animate-ping")).not.toBeNull();

    // The idle project must not produce a status badge.
    expect(
      screen.queryByRole("status", {
        name: /audits running for idle-project/i,
      }),
    ).toBeNull();
  });

  it("renders no badges when no project has running audits", () => {
    useProjectsMock.mockReturnValue({
      data: { items: [IDLE_PROJECT], total: 1 },
      isLoading: false,
      error: null,
    });
    wrap(<ProjectListPage />);

    // No status role, no animate-ping ring anywhere on the page.
    expect(screen.queryByRole("status")).toBeNull();
    expect(document.querySelector(".animate-ping")).toBeNull();
  });
});
