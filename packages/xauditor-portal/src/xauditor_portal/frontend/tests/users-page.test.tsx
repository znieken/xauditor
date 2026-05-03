import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as React from "react";
import UsersPage from "@/app/(authenticated)/users/page";

const useMeMock = vi.fn();
const useUsersMock = vi.fn();
const apiCalls: { fn: string; args: unknown[] }[] = [];

vi.mock("@/lib/api", async () => {
  const actual: Record<string, unknown> = {};
  return {
    ...actual,
    useMe: () => useMeMock(),
    useUsers: (enabled?: boolean) => useUsersMock(enabled),
    api: {
      createUser: (...args: unknown[]) => {
        apiCalls.push({ fn: "createUser", args });
        return Promise.resolve({});
      },
      updateUser: (...args: unknown[]) => {
        apiCalls.push({ fn: "updateUser", args });
        return Promise.resolve({});
      },
      resetUserPassword: (...args: unknown[]) => {
        apiCalls.push({ fn: "resetUserPassword", args });
        return Promise.resolve({});
      },
      deleteUser: (...args: unknown[]) => {
        apiCalls.push({ fn: "deleteUser", args });
        return Promise.resolve(undefined);
      },
    },
    ApiError: class ApiError extends Error {},
  };
});

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("Users page", () => {
  beforeEach(() => {
    useMeMock.mockReset();
    useUsersMock.mockReset();
    apiCalls.length = 0;
    useUsersMock.mockReturnValue({
      data: { items: [], total: 0 },
      refetch: vi.fn(),
      isLoading: false,
    });
  });
  afterEach(() => {
    cleanup();
  });

  it("renders Forbidden for viewers and does not call useUsers", () => {
    useMeMock.mockReturnValue({
      data: { id: "x", username: "v", must_change_password: false, role: "viewer" },
      isLoading: false,
    });
    wrap(<UsersPage />);
    expect(screen.getByText(/Forbidden/i)).toBeInTheDocument();
    // useUsers is rendered but disabled (enabled=false). Verify it was called
    // with `false` so no GET fires.
    expect(useUsersMock).toHaveBeenCalled();
    const lastCall = useUsersMock.mock.calls.at(-1);
    expect(lastCall?.[0]).toBe(false);
  });

  it("renders Forbidden for auditors", () => {
    useMeMock.mockReturnValue({
      data: { id: "x", username: "a", must_change_password: false, role: "auditor" },
      isLoading: false,
    });
    wrap(<UsersPage />);
    expect(screen.getByText(/Forbidden/i)).toBeInTheDocument();
  });

  it("admin sees user list and create form", () => {
    useMeMock.mockReturnValue({
      data: { id: "self", username: "admin", must_change_password: false, role: "admin" },
      isLoading: false,
    });
    useUsersMock.mockReturnValue({
      data: {
        items: [
          {
            id: "self",
            username: "admin",
            role: "admin",
            must_change_password: false,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          },
          {
            id: "alice",
            username: "alice",
            role: "auditor",
            must_change_password: true,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          },
        ],
        total: 2,
      },
      refetch: vi.fn(),
      isLoading: false,
    });
    wrap(<UsersPage />);
    expect(screen.getByText("Create user")).toBeInTheDocument();
    // "admin" appears in multiple places (table username, role chip, role
    // select options); assert we render at least one of them rather than
    // requiring uniqueness.
    expect(screen.getAllByText("admin").length).toBeGreaterThan(0);
    expect(screen.getByText("alice")).toBeInTheDocument();
  });

  it("admin can submit create-user form", async () => {
    useMeMock.mockReturnValue({
      data: { id: "self", username: "admin", must_change_password: false, role: "admin" },
      isLoading: false,
    });
    wrap(<UsersPage />);
    await userEvent.type(screen.getByLabelText("Username"), "newbie");
    await userEvent.type(
      screen.getByLabelText("Initial password"),
      "AbcDef123456",
    );
    await userEvent.click(screen.getByRole("button", { name: /Create/i }));
    await waitFor(() => {
      expect(apiCalls.some((c) => c.fn === "createUser")).toBe(true);
    });
    const call = apiCalls.find((c) => c.fn === "createUser")!;
    expect(call.args[0]).toBe("newbie");
    expect(call.args[2]).toBe("auditor");
  });

  it("disables demote-from-admin and delete on the only admin row", () => {
    useMeMock.mockReturnValue({
      data: { id: "self", username: "admin", must_change_password: false, role: "admin" },
      isLoading: false,
    });
    useUsersMock.mockReturnValue({
      data: {
        items: [
          {
            id: "self",
            username: "admin",
            role: "admin",
            must_change_password: false,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          },
        ],
        total: 1,
      },
      refetch: vi.fn(),
      isLoading: false,
    });
    wrap(<UsersPage />);
    // Scope the assertion to the only-admin row, since the create-user
    // form also renders a Role <select>.
    const adminRow = screen.getByText("admin", { selector: "span" }).closest("tr")!;
    const deleteBtn = within(adminRow).getByRole("button", { name: /Delete user/i });
    expect(deleteBtn).toBeDisabled();
    // Auditor / viewer options on the per-row role select must be disabled.
    const select = within(adminRow).getByRole("combobox");
    const auditorOption = Array.from(select.querySelectorAll("option")).find(
      (o) => o.value === "auditor",
    );
    const viewerOption = Array.from(select.querySelectorAll("option")).find(
      (o) => o.value === "viewer",
    );
    expect(auditorOption).toHaveProperty("disabled", true);
    expect(viewerOption).toHaveProperty("disabled", true);
  });

  it("disables Delete on the row representing the current admin even when other admins exist", () => {
    useMeMock.mockReturnValue({
      data: { id: "self", username: "admin", must_change_password: false, role: "admin" },
      isLoading: false,
    });
    useUsersMock.mockReturnValue({
      data: {
        items: [
          {
            id: "self",
            username: "admin",
            role: "admin",
            must_change_password: false,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          },
          {
            id: "other-admin",
            username: "other-admin",
            role: "admin",
            must_change_password: false,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          },
        ],
        total: 2,
      },
      refetch: vi.fn(),
      isLoading: false,
    });
    wrap(<UsersPage />);
    const selfRow = screen.getByText("admin", { selector: "span" }).closest("tr")!;
    const otherRow = screen
      .getByText("other-admin", { selector: "span" })
      .closest("tr")!;
    // Self row's Delete is gray-out with the "your own account" tooltip;
    // the other admin row's Delete is enabled (we have 2 admins, last-admin
    // guard does not kick in).
    const selfDelete = within(selfRow).getByRole("button", { name: /Delete user/i });
    expect(selfDelete).toBeDisabled();
    expect(selfDelete.getAttribute("title")?.toLowerCase()).toContain(
      "your own account",
    );
    const otherDelete = within(otherRow).getByRole("button", {
      name: /Delete user/i,
    });
    expect(otherDelete).not.toBeDisabled();
  });
});
