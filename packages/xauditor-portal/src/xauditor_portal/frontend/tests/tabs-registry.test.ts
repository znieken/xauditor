import { describe, expect, it } from "vitest";
import { TABS } from "@/lib/tabs";

describe("sidebar tab registry", () => {
  it("registers the Reports tab pointing at /reports with no requiredRole", () => {
    const report = TABS.find((tab) => tab.id === "report");
    expect(report).toMatchObject({
      id: "report",
      label: "Reports",
      route: "/reports",
    });
    expect(report?.icon).toBeTruthy();
    expect(report?.requiredRole).toBeUndefined();
  });

  it("registers an admin-only Users tab pointing at /users", () => {
    const users = TABS.find((tab) => tab.id === "users");
    expect(users).toMatchObject({
      id: "users",
      label: "Users",
      route: "/users",
      requiredRole: "admin",
    });
    expect(users?.icon).toBeTruthy();
  });

  it("does not register a Settings tab", () => {
    const ids = TABS.map((tab) => tab.id);
    const labels = TABS.map((tab) => tab.label);
    const routes = TABS.map((tab) => tab.route);
    expect(ids).not.toContain("settings");
    expect(labels).not.toContain("Settings");
    expect(routes).not.toContain("/settings");
  });
});
