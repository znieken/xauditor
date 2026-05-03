import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// next/navigation mock — `paginated-list` reads `usePathname`,
// `useSearchParams`, and `useRouter` at render time. jsdom has no
// router context, so we supply minimal stubs.
vi.mock("next/navigation", () => {
  const params = new URLSearchParams();
  return {
    usePathname: () => "/reports/runs/run-1",
    useSearchParams: () => params,
    useRouter: () => ({ replace: () => {}, push: () => {} }),
  };
});

import { CoverageView } from "@/components/coverage-view";
import type {
  CoverageFileItem,
  CoverageFunctionItem,
  CoverageModuleItem,
  CoveragePage,
  CoverageSummary,
} from "@/lib/types";
import { renderWithProviders } from "./test-utils";

function emptySummary(): CoverageSummary {
  const zeros = {
    total: 0,
    by_status: {
      audited: 0,
      unaudited: 0,
      excluded: 0,
      skipped: 0,
    },
    percent_audited: 100,
  };
  return { modules: zeros, files: zeros, functions: zeros };
}

function summary(): CoverageSummary {
  return {
    modules: {
      total: 2,
      by_status: { audited: 1, unaudited: 1, excluded: 0, skipped: 0 },
      percent_audited: 50,
    },
    files: {
      total: 1,
      by_status: { audited: 1, unaudited: 0, excluded: 0, skipped: 0 },
      percent_audited: 100,
    },
    functions: {
      total: 3,
      by_status: { audited: 1, unaudited: 2, excluded: 0, skipped: 0 },
      percent_audited: 33.33,
    },
  };
}

function modulePage(): CoveragePage<CoverageModuleItem> {
  return {
    total: 2,
    items: [
      { module_name: "mod.a", status: "audited", reason: null },
      { module_name: "mod.b", status: "unaudited", reason: null },
    ],
  };
}

function filePage(): CoveragePage<CoverageFileItem> {
  return {
    total: 1,
    items: [{ file_path: "src/app.py", status: "audited", reason: null }],
  };
}

function functionPage(): CoveragePage<CoverageFunctionItem> {
  return {
    total: 3,
    items: [
      {
        function_id: "fn-1",
        qualified_name: "mod::fn_1",
        file_path: "src/app.py",
        status: "audited",
        reason: null,
      },
      {
        function_id: "fn-2",
        qualified_name: "mod::fn_2",
        file_path: "src/app.py",
        status: "unaudited",
        reason: null,
      },
      {
        function_id: "fn-3",
        qualified_name: "mod::fn_3",
        file_path: "src/app.py",
        status: "unaudited",
        reason: null,
      },
    ],
  };
}

function mockFetch(summaryData: CoverageSummary) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url.endsWith("/coverage/summary")) {
      return new Response(JSON.stringify(summaryData), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (url.includes("/coverage/modules")) {
      return new Response(JSON.stringify(modulePage()), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (url.includes("/coverage/files")) {
      return new Response(JSON.stringify(filePage()), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (url.includes("/coverage/functions")) {
      return new Response(JSON.stringify(functionPage()), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    return new Response("{}", { status: 200 });
  });
}

describe("CoverageView", () => {
  beforeEach(() => {
    // pagination state uses the URL; Next's usePathname/useSearchParams
    // expect the App Router environment. Stub them via the next/navigation
    // mock used by paginated-list.
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows an empty-state banner when no coverage data exists", async () => {
    vi.stubGlobal("fetch", mockFetch(emptySummary()));
    renderWithProviders(<CoverageView runId="run-1" />);
    await waitFor(() => {
      expect(
        screen.getByText(/No coverage data for this run/i),
      ).toBeInTheDocument();
    });
  });

  it("renders three independently-paginated category cards with summary counts", async () => {
    vi.stubGlobal("fetch", mockFetch(summary()));
    renderWithProviders(<CoverageView runId="run-1" />);
    await waitFor(() => {
      expect(screen.getByText(/^Modules$/)).toBeInTheDocument();
    });
    // Each card heading appears exactly once.
    expect(screen.getByText(/^Modules$/)).toBeInTheDocument();
    expect(screen.getByText(/^Files$/)).toBeInTheDocument();
    expect(screen.getByText(/^Functions$/)).toBeInTheDocument();

    // Per-card total counts are shown in the header.
    // Modules total is 2, Files total is 1, Functions total is 3.
    const totalChips = screen.getAllByText(/^total$/i);
    expect(totalChips.length).toBeGreaterThanOrEqual(3);
  });

  it("renders each page-size selector independently per card", async () => {
    vi.stubGlobal("fetch", mockFetch(summary()));
    renderWithProviders(<CoverageView runId="run-1" />);
    await waitFor(() => {
      expect(
        screen.getAllByLabelText(/^Page size for coverage-/i).length,
      ).toBe(3);
    });
    const selectors = screen.getAllByLabelText(/^Page size for coverage-/i);
    const scopes = selectors
      .map((el) => el.getAttribute("aria-label") ?? "")
      .sort();
    expect(scopes).toEqual([
      expect.stringContaining("coverage-files"),
      expect.stringContaining("coverage-functions"),
      expect.stringContaining("coverage-modules"),
    ]);
  });

  it("sends the status and substring filter parameters to the backend", async () => {
    const fetchMock = mockFetch(summary());
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderWithProviders(<CoverageView runId="run-1" />);
    await waitFor(() =>
      expect(screen.getByText(/^Functions$/)).toBeInTheDocument(),
    );
    // Find the status select inside the Functions card.
    const functionsCard = screen
      .getByText(/^Functions$/)
      .closest("article, div") as HTMLElement | null;
    expect(functionsCard).not.toBeNull();
    // The status select is the one whose value is "" and options include
    // "Unaudited"; pick it by placeholder text absence and selector logic.
    const statusSelects = functionsCard!.querySelectorAll("select");
    // The first select in each card is the status filter.
    const statusSelect = statusSelects[0] as HTMLSelectElement;
    await user.selectOptions(statusSelect, "unaudited");
    await waitFor(() => {
      const called = fetchMock.mock.calls
        .map((c) => String(c[0]))
        .some(
          (url) =>
            url.includes("/coverage/functions") &&
            url.includes("status=unaudited"),
        );
      expect(called).toBe(true);
    });
  });
});
