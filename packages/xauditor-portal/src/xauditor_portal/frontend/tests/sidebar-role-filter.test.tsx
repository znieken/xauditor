import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as React from "react";
import { Sidebar } from "@/components/sidebar";

vi.mock("next/navigation", () => ({
  usePathname: () => "/reports",
}));

const useMeMock = vi.fn();

vi.mock("@/lib/api", () => ({
  useMe: () => useMeMock(),
}));

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("Sidebar role filter", () => {
  beforeEach(() => {
    useMeMock.mockReset();
  });
  afterEach(() => {
    cleanup();
  });

  it("renders the Users tab for admins", () => {
    useMeMock.mockReturnValue({
      data: { id: "x", username: "admin", must_change_password: false, role: "admin" },
    });
    wrap(<Sidebar />);
    expect(screen.getByRole("link", { name: /Users/i })).toBeInTheDocument();
  });

  it("hides the Users tab for auditors", () => {
    useMeMock.mockReturnValue({
      data: {
        id: "x",
        username: "auditor",
        must_change_password: false,
        role: "auditor",
      },
    });
    wrap(<Sidebar />);
    expect(screen.queryByRole("link", { name: /Users/i })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Report/i })).toBeInTheDocument();
  });

  it("hides the Users tab for viewers", () => {
    useMeMock.mockReturnValue({
      data: {
        id: "x",
        username: "viewer",
        must_change_password: false,
        role: "viewer",
      },
    });
    wrap(<Sidebar />);
    expect(screen.queryByRole("link", { name: /Users/i })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Report/i })).toBeInTheDocument();
  });

  it("hides admin-only tabs while role is loading", () => {
    useMeMock.mockReturnValue({ data: undefined });
    wrap(<Sidebar />);
    expect(screen.queryByRole("link", { name: /Users/i })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Report/i })).toBeInTheDocument();
  });
});
