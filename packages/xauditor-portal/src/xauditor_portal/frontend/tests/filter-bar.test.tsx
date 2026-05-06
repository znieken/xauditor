import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { FilterBar } from "@/components/filter-bar";
import type { FindingFilters } from "@/lib/types";

function make(initial: FindingFilters = {}): {
  onChange: ReturnType<typeof vi.fn>;
  onExpandAll: ReturnType<typeof vi.fn>;
  onCollapseAll: ReturnType<typeof vi.fn>;
  rerender: (next: FindingFilters) => void;
} {
  const onChange = vi.fn();
  const onExpandAll = vi.fn();
  const onCollapseAll = vi.fn();
  const utils = render(
    <FilterBar
      value={initial}
      onChange={onChange}
      onExpandAll={onExpandAll}
      onCollapseAll={onCollapseAll}
    />,
  );
  function rerender(next: FindingFilters) {
    utils.rerender(
      <FilterBar
        value={next}
        onChange={onChange}
        onExpandAll={onExpandAll}
        onCollapseAll={onCollapseAll}
      />,
    );
  }
  return { onChange, onExpandAll, onCollapseAll, rerender };
}

describe("FilterBar", () => {
  it("renders every filter control labelled for screen readers", () => {
    make();
    expect(screen.getByLabelText(/file/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/function/i)).toBeInTheDocument();
    // The four enum filters render as multi-select trigger buttons whose
    // aria-label embeds the current selection summary, so we match the
    // prefix of the label rather than the exact text.
    expect(
      screen.getByRole("button", { name: /^Confidence:/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /^Validation:/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /^Exploitation:/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /^Feedback:/i }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText(/search/i)).toBeInTheDocument();
  });

  it("reports text changes upstream via onChange", async () => {
    const user = userEvent.setup();
    const { onChange } = make();
    await user.type(screen.getByLabelText(/^file$/i), "auth");
    expect(onChange).toHaveBeenCalled();
    const last = onChange.mock.calls.at(-1)?.[0];
    expect(last?.file).toBe("auth");
  });

  it("removes a field from the filter object when the input is cleared", async () => {
    const user = userEvent.setup();
    const { onChange } = make({ file: "auth" });
    const input = screen.getByLabelText(/^file$/i) as HTMLInputElement;
    await user.clear(input);
    const last = onChange.mock.calls.at(-1)?.[0];
    expect(last).toBeDefined();
    expect(Object.prototype.hasOwnProperty.call(last, "file")).toBe(false);
  });

  it("wires the Expand-all / Collapse-all / Reset buttons to their callbacks", async () => {
    const user = userEvent.setup();
    const { onExpandAll, onCollapseAll, onChange } = make({ file: "auth" });
    await user.click(screen.getByRole("button", { name: /expand all/i }));
    expect(onExpandAll).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: /collapse all/i }));
    expect(onCollapseAll).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: /reset/i }));
    // Reset returns Validation to the FP-excluding default; the other
    // three multi-selects clear to no selection (omitted from the object).
    const last = onChange.mock.calls.at(-1)?.[0];
    expect(last).toEqual({
      validation_status: ["Valid", "Partial Valid", "Inconclusive"],
    });
  });

  it("Confidence multi-select reports multiple checked values via onChange", async () => {
    const user = userEvent.setup();
    const { onChange, rerender } = make();
    await user.click(screen.getByRole("button", { name: /^Confidence:/i }));
    const popover = screen.getByRole("listbox", { name: /confidence/i });
    await user.click(within(popover).getByText("High"));
    rerender({ confidence: ["High"] });
    await user.click(within(popover).getByText("Medium"));
    const last = onChange.mock.calls.at(-1)?.[0];
    expect(last.confidence).toEqual(["High", "Medium"]);
  });

  it("clearing every Validation option emits an empty array (URL marker)", async () => {
    const user = userEvent.setup();
    const { onChange } = make({ validation_status: ["Valid"] });
    await user.click(screen.getByRole("button", { name: /^Validation:/i }));
    const popover = screen.getByRole("listbox", { name: /validation/i });
    await user.click(within(popover).getByText("Valid"));
    const last = onChange.mock.calls.at(-1)?.[0];
    // The empty array sticks around so the URL serializer can emit
    // ?validation_status= as the "user cleared this filter" marker
    // (suppresses the FP-excluding default on next render).
    expect(last.validation_status).toEqual([]);
  });
});
