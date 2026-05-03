import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { FilterBar } from "@/components/filter-bar";
import type { FindingFilters } from "@/lib/types";

function make(initial: FindingFilters = {}): {
  onChange: ReturnType<typeof vi.fn>;
  onExpandAll: ReturnType<typeof vi.fn>;
  onCollapseAll: ReturnType<typeof vi.fn>;
} {
  const onChange = vi.fn();
  const onExpandAll = vi.fn();
  const onCollapseAll = vi.fn();
  render(
    <FilterBar
      value={initial}
      onChange={onChange}
      onExpandAll={onExpandAll}
      onCollapseAll={onCollapseAll}
    />,
  );
  return { onChange, onExpandAll, onCollapseAll };
}

describe("FilterBar", () => {
  it("renders every filter control labelled for screen readers", () => {
    make();
    expect(screen.getByLabelText(/file/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/function/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/^confidence$/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/validation/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/exploitation/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/feedback/i)).toBeInTheDocument();
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
    const last = onChange.mock.calls.at(-1)?.[0];
    expect(last).toEqual({});
  });
});
