import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FindingCard } from "@/components/finding-card";
import type { FindingDetail, FindingSummary } from "@/lib/types";
import { renderWithProviders } from "./test-utils";

function summary(overrides: Partial<FindingSummary> = {}): FindingSummary {
  return {
    id: "ff-1",
    run_id: "run-1",
    finding_id: "F-001",
    finding_name: "Example vulnerability",
    confidence_level: "High",
    validation_status: "Valid",
    exploitation_status: "exploitable",
    file_path: "src/app.py",
    function_name: "handle_request",
    suspect_line: 42,
    feedback_label: null,
    has_debate: false,
    coder_status: "Skipped",
    created_at: "2026-04-19T00:00:00Z",
    ...overrides,
  };
}

function detailPayload(overrides: Partial<FindingDetail> = {}): FindingDetail {
  return {
    ...summary(),
    finding_description: "short description",
    analyzer_status: null,
    evidence_strength: null,
    analysis: "Analysis prose",
    reason: "Reason prose",
    context: "Context prose",
    business_context: "",
    context_notes: null,
    exploitation_steps: "",
    validation_analysis: "Validation analysis prose",
    source_references: [
      { file_path: "src/app.py", snippet: "print('a')", language: "python", ordinal: 0 },
      { file_path: "src/app.py", snippet: "print('b')", language: "python", ordinal: 1 },
      { file_path: "src/other.py", snippet: "x=1", language: "python", ordinal: 2 },
    ],
    referenced_symbols: [],
    coder_analysis: "",
    coder_reason: "",
    coder_call_chain_evidence: [],
    ...overrides,
  };
}

function mockApi(detail: FindingDetail, opts: { feedback?: boolean } = {}) {
  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const href = typeof url === "string" ? url : url.toString();
    if (href === `/api/findings/${detail.id}`) {
      return new Response(JSON.stringify(detail), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (href === `/api/findings/${detail.id}/duplicates`) {
      // Default: no incoming duplicates. Tests that need a non-empty
      // list can stub fetch directly.
      return new Response(JSON.stringify({ items: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (href === `/api/findings/${detail.id}/feedback`) {
      if (opts.feedback) {
        return new Response(
          JSON.stringify({
            label: "unlabeled",
            researcher_note: null,
            reviewer_username: "auditor",
            updated_at: "2026-04-19T00:00:00Z",
            created_at: "2026-04-19T00:00:00Z",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response(null, { status: 204 });
    }
    return new Response("{}", { status: 200 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("FindingCard", () => {
  beforeEach(() => {
    // next-themes uses localStorage; nothing else to prime.
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders the collapsed header with identifier, name, location, and chips", () => {
    mockApi(detailPayload());
    const s = summary();
    renderWithProviders(
      <FindingCard
        finding={s}
        runId="run-1"
        expanded={false}
        onToggle={() => {}}
      />,
    );
    expect(screen.getByText("F-001")).toBeInTheDocument();
    expect(screen.getByText(/Example vulnerability/)).toBeInTheDocument();
    expect(
      screen.getByText(/handle_request @ src\/app\.py:42/),
    ).toBeInTheDocument();
    const toggle = screen.getByRole("button", { expanded: false });
    expect(toggle).toBeInTheDocument();
  });

  it("calls onToggle(true) when the header is clicked while collapsed", async () => {
    mockApi(detailPayload());
    const onToggle = vi.fn();
    renderWithProviders(
      <FindingCard
        finding={summary()}
        runId="run-1"
        expanded={false}
        onToggle={onToggle}
      />,
    );
    await userEvent.setup().click(screen.getByRole("button", { expanded: false }));
    expect(onToggle).toHaveBeenCalledWith(true);
  });

  it("renders expanded body sections once expanded", async () => {
    mockApi(detailPayload());
    renderWithProviders(
      <FindingCard
        finding={summary()}
        runId="run-1"
        expanded={true}
        onToggle={() => {}}
      />,
    );
    await waitFor(() =>
      expect(screen.getByText(/Analysis prose/)).toBeInTheDocument(),
    );
    // Section headings all rendered
    expect(screen.getByText(/Description/i)).toBeInTheDocument();
    expect(screen.getByText(/^Analysis$/i)).toBeInTheDocument();
    expect(screen.getByText(/^Reason$/i)).toBeInTheDocument();
    expect(screen.getByText(/Source references/i)).toBeInTheDocument();
    expect(screen.getByText(/Validation analysis/i)).toBeInTheDocument();
  });

  it("groups source references into one <details> per file with only the first open", async () => {
    mockApi(detailPayload());
    renderWithProviders(
      <FindingCard
        finding={summary()}
        runId="run-1"
        expanded={true}
        onToggle={() => {}}
      />,
    );
    await waitFor(() =>
      expect(screen.getByText(/Source references/i)).toBeInTheDocument(),
    );
    const summaries = screen.getAllByText(/src\/(app|other)\.py/);
    // One summary per file (app.py appears once, other.py once — ignore the
    // title bar "src/app.py:42" already asserted above).
    expect(summaries.length).toBeGreaterThanOrEqual(2);
    const detailsEls = document.querySelectorAll("details");
    expect(detailsEls.length).toBeGreaterThanOrEqual(2);
    expect((detailsEls[0] as HTMLDetailsElement).open).toBe(true);
    expect((detailsEls[1] as HTMLDetailsElement).open).toBe(false);
  });

  it("shows the amber missing-snippets notice when source_references is empty", async () => {
    mockApi(detailPayload({ source_references: [] }));
    renderWithProviders(
      <FindingCard
        finding={summary()}
        runId="run-1"
        expanded={true}
        onToggle={() => {}}
      />,
    );
    await waitFor(() =>
      expect(
        screen.getByText(/Source snippets not mirrored/i),
      ).toBeInTheDocument(),
    );
    expect(
      screen.getByText(/\.xauditor\/reports/),
    ).toBeInTheDocument();
  });

  it("renders a Debate section for deep-mode findings with has_debate=true", async () => {
    mockApi(detailPayload());
    renderWithProviders(
      <FindingCard
        finding={summary({ has_debate: true })}
        runId="run-1"
        runMode="deep"
        expanded={true}
        onToggle={() => {}}
      />,
    );
    await waitFor(() =>
      expect(screen.getByText(/Debate/i)).toBeInTheDocument(),
    );
    // The "debate" chip on the collapsed header + the section body = two or
    // more mentions of "debate". Do not over-constrain the exact count.
  });

  it("omits the Debate section in fast-mode runs even when has_debate is true", async () => {
    mockApi(detailPayload());
    renderWithProviders(
      <FindingCard
        finding={summary({ has_debate: true })}
        runId="run-1"
        runMode="fast"
        expanded={true}
        onToggle={() => {}}
      />,
    );
    await waitFor(() =>
      expect(screen.getByText(/Analysis prose/)).toBeInTheDocument(),
    );
    // No Debate heading inside the expanded body.
    expect(screen.queryByRole("heading", { name: /debate/i })).toBeNull();
  });

  it("hides the coder chip and section when coder is Skipped (disabled run)", () => {
    mockApi(detailPayload());
    renderWithProviders(
      <FindingCard
        finding={summary({ coder_status: "Skipped" })}
        runId="run-1"
        expanded={false}
        onToggle={() => {}}
      />,
    );
    // Collapsed-card chip is omitted entirely for Skipped findings.
    expect(screen.queryByText(/Coder:/i)).toBeNull();
  });

  it("renders the Coder chip when the verdict is non-Skipped", () => {
    mockApi(detailPayload({ coder_status: "Verified" }));
    renderWithProviders(
      <FindingCard
        finding={summary({ coder_status: "Verified" })}
        runId="run-1"
        expanded={false}
        onToggle={() => {}}
      />,
    );
    expect(screen.getByText(/Coder: Verified/i)).toBeInTheDocument();
  });

  it("renders the Coder chip with Pending while a verification is in flight", () => {
    mockApi(detailPayload({ coder_status: "Pending" }));
    renderWithProviders(
      <FindingCard
        finding={summary({ coder_status: "Pending" })}
        runId="run-1"
        expanded={false}
        onToggle={() => {}}
      />,
    );
    expect(screen.getByText(/Coder: Pending/i)).toBeInTheDocument();
  });

  it("renders the Coder verification body with evidence when expanded and Verified", async () => {
    mockApi(
      detailPayload({
        coder_status: "Verified",
        coder_analysis: "Repo-global review confirms",
        coder_reason: "Sanitizer at src/foo.py:8",
        coder_call_chain_evidence: [
          {
            file_path: "src/foo.py",
            function_name: "sanitize",
            snippet: "def sanitize(): pass",
            language: "python",
            role: "sanitizer",
            ordinal: 0,
          },
        ],
      }),
    );
    renderWithProviders(
      <FindingCard
        finding={summary({ coder_status: "Verified" })}
        runId="run-1"
        expanded={true}
        onToggle={() => {}}
      />,
    );
    await waitFor(() =>
      expect(screen.getAllByText(/Coder verification/i).length).toBeGreaterThan(0),
    );
    expect(
      await screen.findByText(/Repo-global review confirms/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/Sanitizer at src\/foo\.py/i)).toBeInTheDocument();
    expect(screen.getByText(/sanitize/)).toBeInTheDocument();
  });

  it("renders the Coder verification body with a Pending placeholder", async () => {
    mockApi(
      detailPayload({ coder_status: "Pending", coder_analysis: "", coder_reason: "" }),
    );
    renderWithProviders(
      <FindingCard
        finding={summary({ coder_status: "Pending" })}
        runId="run-1"
        expanded={true}
        onToggle={() => {}}
      />,
    );
    await waitFor(() =>
      expect(screen.getByText(/Verification in progress/i)).toBeInTheDocument(),
    );
  });

  // ----- Fail status (multi-project-coder-service §10.6) -----------------

  it("renders the Coder chip with Fail when the dispatch never reached claude", async () => {
    mockApi(
      detailPayload({
        coder_status: "Fail",
        coder_reason: "transport error: 503 Service Unavailable",
      }),
    );
    renderWithProviders(
      <FindingCard
        finding={summary({ coder_status: "Fail" })}
        runId="run-1"
        expanded={false}
        onToggle={() => {}}
      />,
    );
    // The chip text includes the literal "Fail" status.
    expect(screen.getByText(/Coder: Fail/i)).toBeInTheDocument();
  });

  it("Fail chip exposes coder_reason via the title tooltip when expanded", async () => {
    mockApi(
      detailPayload({
        coder_status: "Fail",
        coder_reason: "transport error: 503 Service Unavailable",
      }),
    );
    renderWithProviders(
      <FindingCard
        finding={summary({ coder_status: "Fail" })}
        runId="run-1"
        expanded={true}
        onToggle={() => {}}
      />,
    );
    // The chip's title includes the reason once detail.data has loaded;
    // wait for the API mock to resolve so detail.data.coder_reason is
    // populated.
    const chip = await waitFor(() => {
      const el = screen.getByText(/Coder: Fail/i).closest("span");
      expect(el).not.toBeNull();
      expect(el!.title).toContain("503 Service Unavailable");
      return el!;
    });
    expect(chip.title).toContain("Coder transport failure");
  });

  it("renders the Coder verification body with a transport-failure label when Fail", async () => {
    mockApi(
      detailPayload({
        coder_status: "Fail",
        coder_reason: "transport error: 503 Service Unavailable",
        coder_analysis: "",
        coder_call_chain_evidence: [],
      }),
    );
    renderWithProviders(
      <FindingCard
        finding={summary({ coder_status: "Fail" })}
        runId="run-1"
        expanded={true}
        onToggle={() => {}}
      />,
    );
    // The expanded body explains the failure with the same wording the
    // Markdown export uses, so operators reading either surface have a
    // uniform mental model.
    expect(
      await screen.findByText(/Transport failure — transport error: 503 Service Unavailable/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Not produced \(transport failure\)/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/Resume the run after fixing/i)).toBeInTheDocument();
  });

  it("Fail summary header reads 'Coder verification — Fail'", async () => {
    mockApi(
      detailPayload({
        coder_status: "Fail",
        coder_reason: "transport error: timed out",
      }),
    );
    renderWithProviders(
      <FindingCard
        finding={summary({ coder_status: "Fail" })}
        runId="run-1"
        expanded={true}
        onToggle={() => {}}
      />,
    );
    // The <details><summary> reads "Coder verification — Fail" so
    // reviewers see the verdict before expanding.
    expect(
      await screen.findByText(/Coder verification — Fail/i),
    ).toBeInTheDocument();
  });
});
