import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StagesFormChip } from "@/components/status-chip";

describe("StagesFormChip", () => {
  it("renders 'Stages: prompt' for the prompt form by default", () => {
    render(<StagesFormChip stages_form="prompt" />);
    expect(screen.getByText("Stages: prompt")).toBeInTheDocument();
  });

  it("renders 'Stages: agentic' for the agentic form by default", () => {
    render(<StagesFormChip stages_form="agentic" />);
    expect(screen.getByText("Stages: agentic")).toBeInTheDocument();
  });

  it("drops the 'Stages:' prefix in compact mode (run-list table)", () => {
    render(<StagesFormChip stages_form="prompt" compact />);
    expect(screen.getByText("prompt")).toBeInTheDocument();
    expect(screen.queryByText(/Stages:/)).not.toBeInTheDocument();
  });

  it("renders compact 'agentic' for the agentic form", () => {
    render(<StagesFormChip stages_form="agentic" compact />);
    expect(screen.getByText("agentic")).toBeInTheDocument();
  });
});
