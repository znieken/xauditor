import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { ThemeToggle } from "@/components/theme-toggle";
import { renderWithProviders } from "./test-utils";

describe("ThemeToggle", () => {
  it("renders an accessible button with the current theme", () => {
    renderWithProviders(<ThemeToggle />, { theme: "light" });
    const button = screen.getByRole("button", { name: /theme:/i });
    expect(button).toBeInTheDocument();
  });

  it("cycles light → dark → system → light on repeated clicks", async () => {
    const user = userEvent.setup();
    renderWithProviders(<ThemeToggle />, { theme: "light" });
    const button = screen.getByRole("button", { name: /theme:/i });

    expect(button.getAttribute("aria-label")).toMatch(/theme: light/i);

    await user.click(button);
    expect(button.getAttribute("aria-label")).toMatch(/theme: dark/i);

    await user.click(button);
    expect(button.getAttribute("aria-label")).toMatch(/theme: system/i);

    await user.click(button);
    expect(button.getAttribute("aria-label")).toMatch(/theme: light/i);
  });
});
