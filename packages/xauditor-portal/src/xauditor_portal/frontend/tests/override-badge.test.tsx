import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { OverrideBadge } from "@/components/override-badge";

describe("OverrideBadge", () => {
  it("renders the yml override label", () => {
    render(<OverrideBadge ymlKey="logging.level" />);
    expect(screen.getByText(/yml override/i)).toBeInTheDocument();
  });

  it("exposes the overriding yml key in the tooltip", () => {
    render(<OverrideBadge ymlKey="llm.providers.shared.temperature" />);
    const badge = screen.getByText(/yml override/i).closest("span");
    expect(badge).not.toBeNull();
    expect(badge!.getAttribute("title")).toContain(
      "llm.providers.shared.temperature",
    );
  });
});
