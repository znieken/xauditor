import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ChipInput } from "@/components/chip-input";

function harness(initial: string[] = [], extra: Record<string, unknown> = {}) {
  const onChange = vi.fn();
  const utils = render(
    <ChipInput value={initial} onChange={onChange} {...extra} />,
  );
  return { onChange, ...utils };
}

describe("ChipInput", () => {
  it("renders existing chips", () => {
    harness(["alpha", "beta"]);
    expect(screen.getByText("alpha")).toBeInTheDocument();
    expect(screen.getByText("beta")).toBeInTheDocument();
  });

  it("adds a chip on Enter", async () => {
    const user = userEvent.setup();
    const { onChange } = harness([]);
    await user.click(screen.getByRole("textbox"));
    await user.keyboard("foo{Enter}");
    expect(onChange).toHaveBeenCalledWith(["foo"]);
  });

  it("adds a chip on comma keystroke", async () => {
    const user = userEvent.setup();
    const { onChange } = harness([]);
    await user.click(screen.getByRole("textbox"));
    await user.keyboard("bar,");
    expect(onChange).toHaveBeenCalledWith(["bar"]);
  });

  it("adds a chip on blur", async () => {
    const user = userEvent.setup();
    const { onChange } = harness([]);
    await user.click(screen.getByRole("textbox"));
    await user.keyboard("baz");
    fireEvent.blur(screen.getByRole("textbox"));
    expect(onChange).toHaveBeenCalledWith(["baz"]);
  });

  it("dedupes when adding an existing chip", async () => {
    const user = userEvent.setup();
    const { onChange } = harness(["foo"]);
    await user.click(screen.getByRole("textbox"));
    await user.keyboard("foo{Enter}");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("removes the trailing chip on Backspace when input is empty", async () => {
    const user = userEvent.setup();
    const { onChange } = harness(["alpha", "beta"]);
    await user.click(screen.getByRole("textbox"));
    await user.keyboard("{Backspace}");
    expect(onChange).toHaveBeenCalledWith(["alpha"]);
  });

  it("removes a chip via its remove button", async () => {
    const user = userEvent.setup();
    const { onChange } = harness(["alpha", "beta"]);
    await user.click(screen.getByLabelText("Remove alpha"));
    expect(onChange).toHaveBeenCalledWith(["beta"]);
  });

  it("splits pasted content on , / newline / ;", () => {
    const { onChange } = harness([]);
    const input = screen.getByRole("textbox") as HTMLInputElement;
    fireEvent.paste(input, {
      clipboardData: { getData: () => "one, two\nthree;four" },
    });
    expect(onChange).toHaveBeenCalledWith(["one", "two", "three", "four"]);
  });

  it("renders invalid chips in a rose tone with the validator message", () => {
    const validate = (chip: string) =>
      chip === "bad" ? "Unknown provider 'bad'" : null;
    harness(["good", "bad"], { validate });
    // The chip wrapper is the outer <span> with title=. Find each by its
    // inner text, then walk up to the wrapper that carries the title attribute.
    const goodWrapper = screen
      .getByText("good")
      .parentElement as HTMLElement;
    const badWrapper = screen
      .getByText("bad")
      .parentElement as HTMLElement;
    expect(goodWrapper.className).not.toMatch(/rose/);
    expect(badWrapper.className).toMatch(/rose/);
    expect(badWrapper.getAttribute("title")).toBe("Unknown provider 'bad'");
  });

  it("filters suggestions case-insensitively and adds on click", async () => {
    const user = userEvent.setup();
    const { onChange } = harness([], {
      suggestions: ["claude", "gpt", "llama"],
    });
    await user.click(screen.getByRole("textbox"));
    await user.keyboard("CL");
    const option = await screen.findByRole("option", { name: "claude" });
    fireEvent.mouseDown(option);
    expect(onChange).toHaveBeenCalledWith(["claude"]);
  });

  it("disabled state hides remove buttons and ignores typing", () => {
    const { onChange } = harness(["alpha"], { disabled: true });
    expect(screen.queryByLabelText("Remove alpha")).toBeNull();
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
    expect(onChange).not.toHaveBeenCalled();
  });
});
