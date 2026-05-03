import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FeedbackControl } from "@/components/feedback-control";
import type { FeedbackPayload } from "@/lib/types";
import { renderWithProviders } from "./test-utils";

function mockFetchJson(status: number, body: unknown) {
  // Node 24+ rejects ``new Response("", {status: 204})`` per the spec
  // (204/304 must have a null body). Pass ``null`` for empty bodies and
  // strip the content-type header so the consumer's empty-text branch
  // still triggers without tripping the standards check.
  const isEmpty = body === undefined;
  return vi.fn().mockResolvedValue(
    new Response(
      isEmpty ? null : JSON.stringify(body),
      isEmpty
        ? { status }
        : { status, headers: { "Content-Type": "application/json" } },
    ),
  );
}

describe("FeedbackControl", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    vi.stubGlobal("fetch", mockFetchJson(204, undefined));
  });

  afterEach(() => {
    if (originalFetch) vi.stubGlobal("fetch", originalFetch);
  });

  it("renders the three label buttons with the current state reflected", () => {
    const current: FeedbackPayload = {
      label: "true_positive",
      researcher_note: null,
      reviewer_username: "auditor",
      updated_at: "2026-04-19T00:00:00Z",
      created_at: "2026-04-19T00:00:00Z",
    };
    renderWithProviders(
      <FeedbackControl findingId="f-1" current={current} />,
    );
    const truePositive = screen.getByRole("button", { name: /true positive/i });
    expect(truePositive.getAttribute("aria-pressed")).toBe("true");
    const falsePositive = screen.getByRole("button", {
      name: /false positive/i,
    });
    expect(falsePositive.getAttribute("aria-pressed")).toBe("false");
  });

  it("POSTs a new feedback label when none exists yet", async () => {
    const fetchMock = mockFetchJson(200, {
      label: "false_positive",
      researcher_note: null,
      reviewer_username: "auditor",
      updated_at: "2026-04-19T00:00:00Z",
      created_at: "2026-04-19T00:00:00Z",
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderWithProviders(
      <FeedbackControl findingId="f-1" current={null} />,
    );
    await user.click(
      screen.getByRole("button", { name: /false positive/i }),
    );
    await waitFor(() => {
      const feedbackCall = fetchMock.mock.calls.find(
        (args) => String(args[0]) === "/api/findings/f-1/feedback",
      );
      expect(feedbackCall).toBeDefined();
    });
    const feedbackCall = fetchMock.mock.calls.find(
      (args) => String(args[0]) === "/api/findings/f-1/feedback",
    ) as [string, RequestInit];
    expect(feedbackCall[1].method).toBe("POST");
    expect(JSON.parse(String(feedbackCall[1].body))).toMatchObject({
      label: "false_positive",
    });
  });

  it("PATCHes when a feedback record already exists", async () => {
    const fetchMock = mockFetchJson(200, {
      label: "true_positive",
      researcher_note: "note",
      reviewer_username: "auditor",
      updated_at: "2026-04-19T00:00:00Z",
      created_at: "2026-04-19T00:00:00Z",
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    const current: FeedbackPayload = {
      label: "unlabeled",
      researcher_note: null,
      reviewer_username: "auditor",
      updated_at: "2026-04-19T00:00:00Z",
      created_at: "2026-04-19T00:00:00Z",
    };
    renderWithProviders(
      <FeedbackControl findingId="f-1" current={current} />,
    );
    await user.click(
      screen.getByRole("button", { name: /true positive/i }),
    );
    await waitFor(() => {
      const feedbackCall = fetchMock.mock.calls.find(
        (args) => String(args[0]) === "/api/findings/f-1/feedback",
      );
      expect(feedbackCall).toBeDefined();
    });
    const feedbackCall = fetchMock.mock.calls.find(
      (args) => String(args[0]) === "/api/findings/f-1/feedback",
    ) as [string, RequestInit];
    expect(feedbackCall[1].method).toBe("PATCH");
  });

  it("renders the duplicate-confirmation hint below the Duplicate button", () => {
    renderWithProviders(
      <FeedbackControl findingId="f-1" current={null} />,
    );
    const hint = screen.getByText(
      /only mark as duplicate after confirming this is a real \(valid\) finding\./i,
    );
    expect(hint).toBeInTheDocument();
    const duplicateBtn = screen.getByRole("button", {
      name: /^duplicate$/i,
    });
    expect(
      duplicateBtn.compareDocumentPosition(hint) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("refuses to save a note while the label is unlabeled", async () => {
    const fetchMock = mockFetchJson(200, {});
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderWithProviders(
      <FeedbackControl findingId="f-1" current={null} />,
    );
    await user.click(screen.getByRole("button", { name: /save note/i }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent?.toLowerCase()).toContain("before saving");
    // The component fires `useMe` so a /api/auth/me call is expected;
    // assert that NO feedback-write request was issued.
    const feedbackCall = fetchMock.mock.calls.find(
      (args) => String(args[0]).startsWith("/api/findings/"),
    );
    expect(feedbackCall).toBeUndefined();
  });

  describe("Role gating", () => {
    function fetchWithMe(role: "admin" | "auditor" | "viewer") {
      return vi.fn(async (url: RequestInfo | URL) => {
        const path = typeof url === "string" ? url : url.toString();
        if (path === "/api/auth/me") {
          return new Response(
            JSON.stringify({
              id: "u",
              username: "actor",
              must_change_password: false,
              role,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response("{}", {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      });
    }

    it("renders disabled buttons and read-only note for viewers", async () => {
      const fetchMock = fetchWithMe("viewer");
      vi.stubGlobal("fetch", fetchMock);
      renderWithProviders(<FeedbackControl findingId="f-1" current={null} />);
      // Wait for `useMe` to resolve so the read-only state takes effect.
      await waitFor(() => {
        expect(
          screen
            .getByRole("button", { name: /true positive/i })
            .hasAttribute("disabled"),
        ).toBe(true);
      });
      const fp = screen.getByRole("button", { name: /false positive/i });
      const dup = screen.getByRole("button", { name: /^duplicate$/i });
      const unl = screen.getByRole("button", { name: /unlabeled/i });
      expect(fp).toBeDisabled();
      expect(dup).toBeDisabled();
      expect(unl).toBeDisabled();
      const note = screen.getByLabelText(/researcher note/i) as HTMLTextAreaElement;
      expect(note).toBeDisabled();
      const saveNote = screen.getByRole("button", { name: /save note/i });
      expect(saveNote).toBeDisabled();
    });

    it("does not POST when a viewer clicks a label button", async () => {
      const fetchMock = fetchWithMe("viewer");
      vi.stubGlobal("fetch", fetchMock);
      const user = userEvent.setup();
      renderWithProviders(<FeedbackControl findingId="f-1" current={null} />);
      await waitFor(() => {
        expect(
          screen
            .getByRole("button", { name: /true positive/i })
            .hasAttribute("disabled"),
        ).toBe(true);
      });
      await user.click(screen.getByRole("button", { name: /true positive/i }));
      // The only fetch we expect is the /api/auth/me call.
      const writeCalls = fetchMock.mock.calls.filter((args) => {
        const u = args[0] as string;
        return u.startsWith("/api/findings/");
      });
      expect(writeCalls).toHaveLength(0);
    });

    it("renders enabled buttons for auditors", async () => {
      const fetchMock = fetchWithMe("auditor");
      vi.stubGlobal("fetch", fetchMock);
      renderWithProviders(<FeedbackControl findingId="f-1" current={null} />);
      await waitFor(() => {
        // useMe has resolved when the textarea is no longer in the read-only
        // placeholder state. Use the button's `disabled` attribute as proxy.
        expect(
          screen
            .getByRole("button", { name: /true positive/i })
            .hasAttribute("disabled"),
        ).toBe(false);
      });
      const tp = screen.getByRole("button", { name: /true positive/i });
      expect(tp).not.toBeDisabled();
    });
  });
});
