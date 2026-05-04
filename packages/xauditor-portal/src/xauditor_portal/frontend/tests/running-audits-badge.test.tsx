import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { RunningAuditsBadge } from "@/components/running-audits-badge";

describe("RunningAuditsBadge", () => {
  it("renders nothing when count is 0", () => {
    const { container } = render(
      <RunningAuditsBadge count={0} aria-label="0 running" />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when count is negative", () => {
    const { container } = render(
      <RunningAuditsBadge count={-1} aria-label="negative" />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders the integer count and an animated ring when count >= 1", () => {
    render(
      <RunningAuditsBadge count={3} aria-label="3 audits running for foo" />,
    );
    const status = screen.getByRole("status", {
      name: /3 audits running for foo/i,
    });
    expect(status).toBeInTheDocument();
    expect(status.textContent).toBe("3");
    // The pulsing ring is the absolutely-positioned sibling decorated
    // with `animate-ping`.
    const ring = status.querySelector(".animate-ping");
    expect(ring).not.toBeNull();
    expect(ring?.getAttribute("aria-hidden")).toBe("true");
  });
});
