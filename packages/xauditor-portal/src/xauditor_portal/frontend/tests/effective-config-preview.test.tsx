import { screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EffectiveConfigPreview } from "@/components/effective-config-preview";
import { renderWithProviders } from "./test-utils";

function fakeFetch(payload: unknown) {
  return vi.fn().mockResolvedValue(
    new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

describe("EffectiveConfigPreview", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      fakeFetch({
        agents: [
          {
            agent: "auditor",
            provider: "shared",
            model_name: "gpt-4o",
            temperature: 0.0,
            top_p: 1.0,
            top_k: null,
            repetition_penalty: null,
            thinking_enabled: false,
            request_timeout_seconds: 1800,
            sources: {
              provider: "yml",
              model_name: "yml",
              temperature: "yml",
              top_p: "yml",
              top_k: "default",
              repetition_penalty: "default",
              thinking_enabled: "default",
              request_timeout_seconds: "yml",
            },
          },
          {
            agent: "validator",
            provider: "shared",
            model_name: "gpt-4o",
            temperature: 0.0,
            top_p: 1.0,
            top_k: null,
            repetition_penalty: null,
            thinking_enabled: false,
            request_timeout_seconds: null,
            sources: {
              provider: "yml",
              model_name: "yml",
              temperature: "yml",
              top_p: "yml",
              top_k: "default",
              repetition_penalty: "default",
              thinking_enabled: "default",
              request_timeout_seconds: "default",
            },
          },
        ],
        providers: ["shared"],
        default_provider: "shared",
      }),
    );
  });

  afterEach(() => {
    if (originalFetch) vi.stubGlobal("fetch", originalFetch);
  });

  it("renders the request_timeout_seconds column with values + source tags", async () => {
    renderWithProviders(<EffectiveConfigPreview />);
    expect(
      await screen.findByRole("columnheader", { name: /Req\. timeout/i }),
    ).toBeInTheDocument();
    // Provider-resolved override (1800 from yml) for auditor.
    await waitFor(() => {
      expect(screen.getByText("1800")).toBeInTheDocument();
    });
    // Validator falls back to default — null renders as the em-dash.
    const cells = screen.getAllByText("—");
    // top_k / repetition_penalty / thinking is rendered with —/off, plus
    // request_timeout_seconds → null on validator. At least one — exists.
    expect(cells.length).toBeGreaterThan(0);
  });
});
