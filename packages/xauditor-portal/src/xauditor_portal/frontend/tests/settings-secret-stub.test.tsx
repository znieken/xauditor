import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CoderSection } from "@/components/coder-section";
import type { ConfigField, RedactedKey } from "@/lib/types";

function withTransport(): Record<string, ConfigField> {
  return {
    "coder.transport": {
      value: "subprocess",
      source: "default",
      overridden_by_yml: false,
    },
  };
}

describe("Secret stubs render based on redacted_keys", () => {
  it("renders 'configured' when present is true", () => {
    const redactedKeys: Record<string, RedactedKey> = {
      "coder.model_api_key": { present: true, source: "yml" },
      "coder.endpoint_token": { present: true, source: "yml" },
    };
    render(
      <CoderSection
        fields={withTransport()}
        redactedKeys={redactedKeys}
        draft={{}}
        onChange={vi.fn()}
      />,
    );
    const configured = screen.getAllByText("configured");
    expect(configured.length).toBe(2);
  });

  it("renders 'not configured' when present is false", () => {
    const redactedKeys: Record<string, RedactedKey> = {
      "coder.model_api_key": { present: false, source: "default" },
      "coder.endpoint_token": { present: false, source: "default" },
    };
    render(
      <CoderSection
        fields={withTransport()}
        redactedKeys={redactedKeys}
        draft={{}}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getAllByText("not configured").length).toBe(2);
  });

  it("renders the source tag alongside the stub", () => {
    const redactedKeys: Record<string, RedactedKey> = {
      "coder.model_api_key": { present: true, source: "yml" },
      "coder.endpoint_token": { present: false, source: "default" },
    };
    render(
      <CoderSection
        fields={withTransport()}
        redactedKeys={redactedKeys}
        draft={{}}
        onChange={vi.fn()}
      />,
    );
    // The page renders many `src: ...` tags (one per field); we only need
    // to assert that *both* secret stubs surface their source. Find the
    // unique stub container via its label, then assert the src tag inside it.
    const apiKeyLabel = screen.getByText("Model API key");
    const apiKeyContainer = apiKeyLabel.closest("div")?.parentElement as HTMLElement;
    expect(apiKeyContainer.textContent).toContain("src: yml");

    const tokenLabel = screen.getByText("Endpoint token");
    const tokenContainer = tokenLabel.closest("div")?.parentElement as HTMLElement;
    expect(tokenContainer.textContent).toContain("src: default");
  });
});
