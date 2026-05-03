import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SettingsToc } from "@/components/settings-toc";

describe("SettingsToc", () => {
  it("renders one anchor per entry pointing at #<id>", () => {
    render(
      <SettingsToc
        entries={[
          { id: "section-logging", label: "Logging" },
          { id: "section-coder", label: "Coder" },
        ]}
      />,
    );
    const logging = screen.getByRole("link", { name: /Logging/ });
    expect(logging.getAttribute("href")).toBe("#section-logging");
    const coder = screen.getByRole("link", { name: /Coder/ });
    expect(coder.getAttribute("href")).toBe("#section-coder");
  });

  it("shows badge counts > 0 and hides them at 0", () => {
    render(
      <SettingsToc
        entries={[
          { id: "a", label: "A", badgeCount: 3 },
          { id: "b", label: "B", badgeCount: 0 },
        ]}
      />,
    );
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.queryByText("0")).toBeNull();
  });
});
