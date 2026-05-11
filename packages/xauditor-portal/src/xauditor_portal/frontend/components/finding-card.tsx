"use client";

import { ChevronDown, ChevronRight } from "lucide-react";
import * as React from "react";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark, oneLight } from "react-syntax-highlighter/dist/esm/styles/prism";
import { useTheme } from "next-themes";
import { api, useFinding, useIncomingDuplicates } from "@/lib/api";
import { cn, firstLine, truncate } from "@/lib/utils";
import {
  Badge,
  coderTone,
  confidenceTone,
  exploitationTone,
  feedbackTone,
  validationTone,
} from "@/components/ui/badge";
import { FeedbackControl } from "@/components/feedback-control";
import { FindingDebateSection } from "@/components/finding-debate-section";
import { PerUnitVerdictsPanel } from "@/components/per-unit-verdicts";
import type { FeedbackPayload, FindingSummary } from "@/lib/types";

interface Props {
  finding: FindingSummary;
  runId: string;
  runMode?: "fast" | "deep";
  expanded: boolean;
  onToggle: (next: boolean) => void;
}

interface SourceGroup {
  file_path: string;
  items: Array<{
    snippet: string;
    language: string | null;
    ordinal: number;
  }>;
}

function groupReferencesByFile(
  refs: Array<{
    file_path: string;
    snippet: string;
    language: string | null;
    ordinal: number;
  }>,
): SourceGroup[] {
  const groups = new Map<string, SourceGroup>();
  const sorted = [...refs].sort((a, b) => a.ordinal - b.ordinal);
  for (const ref of sorted) {
    const key = ref.file_path || "(unknown file)";
    let group = groups.get(key);
    if (!group) {
      group = { file_path: key, items: [] };
      groups.set(key, group);
    }
    group.items.push({
      snippet: ref.snippet,
      language: ref.language,
      ordinal: ref.ordinal,
    });
  }
  return Array.from(groups.values());
}

export function FindingCard({
  finding,
  runId,
  runMode,
  expanded,
  onToggle,
}: Props) {
  const { resolvedTheme } = useTheme();
  const darkSyntax = resolvedTheme === "dark";
  const detail = useFinding(finding.id);
  // Reverse-query: list of findings that point AT this one as
  // their canonical duplicate target. Lazy-loaded — only fires
  // when the card is expanded so the runs / findings list page
  // stays cheap (avoids N+1).
  const incoming = useIncomingDuplicates(finding.id, expanded);
  const [feedback, setFeedback] = React.useState<
    FeedbackPayload | null | undefined
  >(undefined);

  React.useEffect(() => {
    if (!expanded) return;
    let cancelled = false;
    api.getFeedback(finding.id).then(
      (value) => {
        if (!cancelled) setFeedback(value ?? null);
      },
      () => {
        if (!cancelled) setFeedback(null);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [expanded, finding.id]);

  const title =
    finding.function_name && finding.file_path
      ? `${finding.function_name} @ ${finding.file_path}:${finding.suspect_line ?? "?"}`
      : finding.file_path
      ? `${finding.file_path}:${finding.suspect_line ?? "?"}`
      : finding.finding_id;

  const showDebate = runMode === "deep" && finding.has_debate;
  const sourceGroups = detail.data
    ? groupReferencesByFile(detail.data.source_references)
    : [];

  return (
    <article
      className={cn(
        "rounded-xl border border-zinc-200 bg-white transition-shadow hover:shadow-sm dark:border-zinc-800 dark:bg-zinc-900/60",
        expanded && "shadow-sm",
      )}
    >
      <button
        type="button"
        className="flex w-full items-start gap-3 px-4 py-3 text-left"
        onClick={() => onToggle(!expanded)}
        aria-expanded={expanded}
      >
        <div className="mt-0.5 text-zinc-400">
          {expanded ? (
            <ChevronDown className="h-4 w-4" aria-hidden />
          ) : (
            <ChevronRight className="h-4 w-4" aria-hidden />
          )}
        </div>
        <div className="flex flex-1 flex-col gap-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-xs text-zinc-500 dark:text-zinc-400">
              {finding.finding_id}
            </span>
            <span className="font-medium text-zinc-900 dark:text-zinc-100">
              {finding.finding_name}
            </span>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-xs text-zinc-500 dark:text-zinc-400">
            <span className="font-mono">{title}</span>
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge tone={confidenceTone(finding.confidence_level)}>
              Confidence: {finding.confidence_level}
            </Badge>
            <Badge tone={validationTone(finding.validation_status)}>
              {finding.validation_status}
            </Badge>
            <Badge tone={exploitationTone(finding.exploitation_status)}>
              {finding.exploitation_status}
            </Badge>
            {finding.feedback_label === "duplicate" ? (
              <Badge tone="info">
                {detail.data?.duplicate_of
                  ? `Duplicate of ${detail.data.duplicate_of.finding_id}`
                  : "Duplicate"}
              </Badge>
            ) : (
              <Badge tone={feedbackTone(finding.feedback_label)}>
                Feedback: {finding.feedback_label ?? "unlabeled"}
              </Badge>
            )}
            {showDebate ? <Badge tone="info">debate</Badge> : null}
            {finding.coder_status !== "Skipped" ? (
              <Badge
                tone={coderTone(finding.coder_status)}
                title={
                  finding.coder_status === "Fail"
                    ? `Coder transport failure: ${detail.data?.coder_reason ?? "(see audit log)"}`
                    : undefined
                }
              >
                {finding.coder_status === "Fail" ? "⚡ " : null}
                Coder: {finding.coder_status}
              </Badge>
            ) : null}
          </div>
          {!expanded && detail.data ? (
            <p className="mt-1 text-xs text-zinc-600 dark:text-zinc-400">
              {truncate(firstLine(detail.data.analysis), 180)}
            </p>
          ) : null}
        </div>
      </button>
      {expanded ? (
        <div className="border-t border-zinc-100 px-4 py-4 dark:border-zinc-800">
          {detail.isLoading ? (
            <p className="text-xs text-zinc-500">Loading finding details…</p>
          ) : detail.data ? (
            <div className="space-y-4 text-sm">
              <Section title="Description">
                <p className="whitespace-pre-wrap leading-relaxed">
                  {detail.data.finding_description}
                </p>
              </Section>
              <Section title="Analysis">
                <p className="whitespace-pre-wrap leading-relaxed">
                  {detail.data.analysis}
                </p>
              </Section>
              <Section title="Reason">
                <p className="whitespace-pre-wrap leading-relaxed">
                  {detail.data.reason}
                </p>
              </Section>
              <Section title="Context">
                <p className="whitespace-pre-wrap leading-relaxed">
                  {detail.data.context}
                </p>
              </Section>
              {detail.data.context_notes ? (
                <Section title="Context notes">
                  <p className="whitespace-pre-wrap leading-relaxed">
                    {detail.data.context_notes}
                  </p>
                </Section>
              ) : null}
              <Section title="Source references">
                {sourceGroups.length === 0 ? (
                  <MissingSnippetsNotice />
                ) : (
                  <div className="space-y-2">
                    {sourceGroups.map((group, idx) => (
                      <details
                        key={group.file_path}
                        className="overflow-hidden rounded-md border border-zinc-200 dark:border-zinc-800"
                        open={idx === 0}
                      >
                        <summary className="cursor-pointer select-none bg-zinc-50 px-3 py-1.5 font-mono text-[11px] text-zinc-600 dark:bg-zinc-900 dark:text-zinc-300">
                          {group.file_path}
                          <span className="ml-2 text-zinc-400">
                            ({group.items.length} snippet
                            {group.items.length === 1 ? "" : "s"})
                          </span>
                        </summary>
                        <div className="space-y-2 border-t border-zinc-200 px-0 py-0 dark:border-zinc-800">
                          {group.items.map((item) => (
                            <SyntaxHighlighter
                              key={`${group.file_path}-${item.ordinal}`}
                              language={item.language ?? "text"}
                              style={darkSyntax ? oneDark : oneLight}
                              customStyle={{
                                margin: 0,
                                fontSize: "12px",
                                padding: "12px",
                              }}
                              wrapLongLines
                            >
                              {item.snippet}
                            </SyntaxHighlighter>
                          ))}
                        </div>
                      </details>
                    ))}
                  </div>
                )}
              </Section>
              {detail.data.exploitation_steps ? (
                <Section title="Exploitation steps">
                  <pre className="whitespace-pre-wrap rounded-md bg-zinc-50 p-3 text-xs leading-relaxed dark:bg-zinc-950/60">
                    {detail.data.exploitation_steps}
                  </pre>
                </Section>
              ) : null}
              <Section title="Validation analysis">
                <p className="whitespace-pre-wrap leading-relaxed">
                  {detail.data.validation_analysis}
                </p>
                <PerUnitVerdictsPanel
                  reconciliation={detail.data.reconciliation}
                />
              </Section>
              {detail.data.referenced_symbols.length > 0 ? (
                <Section title="Referenced symbols">
                  <ul className="space-y-1 text-xs">
                    {detail.data.referenced_symbols.map((s, idx) => (
                      <li
                        key={idx}
                        className="font-mono text-zinc-600 dark:text-zinc-300"
                      >
                        {String((s as Record<string, unknown>).name ?? "(unknown)")}
                      </li>
                    ))}
                  </ul>
                </Section>
              ) : null}
              {finding.coder_status !== "Skipped" ||
              detail.data.coder_status !== "Skipped" ? (
                <CoderVerificationDetails
                  detail={detail.data}
                  darkSyntax={darkSyntax}
                />
              ) : null}
              {showDebate ? (
                <Section title="Debate">
                  <FindingDebateSection
                    runId={runId}
                    findingId={finding.id}
                  />
                </Section>
              ) : null}
              {incoming.data && incoming.data.items.length > 0 ? (
                <Section title="Marked as duplicate by">
                  <ul className="space-y-1 text-xs">
                    {incoming.data.items.map((item) => (
                      <li key={item.id}>
                        <span className="font-mono text-zinc-600 dark:text-zinc-400">
                          {item.finding_id}
                        </span>
                        <span className="ml-2 text-zinc-900 dark:text-zinc-100">
                          {item.name}
                        </span>
                      </li>
                    ))}
                  </ul>
                </Section>
              ) : null}
              <Section title="Feedback">
                <FeedbackControl
                  findingId={finding.id}
                  runId={runId}
                  current={feedback}
                  onChanged={() =>
                    api.getFeedback(finding.id).then(
                      (value) => setFeedback(value ?? null),
                      () => {},
                    )
                  }
                />
              </Section>
            </div>
          ) : (
            <p className="text-xs text-rose-600">Failed to load finding.</p>
          )}
        </div>
      ) : null}
    </article>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section>
      <h4 className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
        {title}
      </h4>
      <div className="text-zinc-800 dark:text-zinc-200">{children}</div>
    </section>
  );
}

function MissingSnippetsNotice() {
  return (
    <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-200">
      <p className="font-medium">Source snippets not mirrored.</p>
      <p className="mt-0.5">
        The audit produced <code className="font-mono">findings.md</code> with
        source code, but the Postgres sink did not record it. This indicates a
        sink-mirror gap for this run. Inspect the Markdown artifact directly
        under{" "}
        <code className="font-mono">.xauditor/reports/&lt;timestamp&gt;/</code>.
      </p>
    </div>
  );
}

function CoderVerificationDetails({
  detail,
  darkSyntax,
}: {
  detail: import("@/lib/types").FindingDetail;
  darkSyntax: boolean;
}) {
  const isPending = detail.coder_status === "Pending";
  const isSkipped = detail.coder_status === "Skipped";
  const isFail = detail.coder_status === "Fail";
  return (
    <details className="overflow-hidden rounded-md border border-zinc-200 dark:border-zinc-800">
      <summary className="cursor-pointer select-none bg-zinc-50 px-3 py-1.5 text-xs text-zinc-700 dark:bg-zinc-900 dark:text-zinc-300">
        <span className="font-medium">Coder verification — {detail.coder_status}</span>
        {detail.coder_call_chain_evidence.length > 0 ? (
          <span className="ml-2 text-zinc-400">
            ({detail.coder_call_chain_evidence.length} evidence item
            {detail.coder_call_chain_evidence.length === 1 ? "" : "s"})
          </span>
        ) : null}
      </summary>
      <div className="space-y-3 px-3 py-3 text-xs">
        {isPending ? (
          <p className="italic text-zinc-500 dark:text-zinc-400">
            Verification in progress. Refreshes automatically when the coder task
            settles.
          </p>
        ) : null}
        {isSkipped ? (
          <p className="italic text-zinc-500 dark:text-zinc-400">
            Verification skipped. {detail.coder_reason ? `Reason: ${detail.coder_reason}` : ""}
          </p>
        ) : null}
        {isFail ? (
          <div className="space-y-1">
            <p className="font-medium text-rose-700 dark:text-rose-400">
              Transport failure — {detail.coder_reason || "(see audit log)"}
            </p>
            <p className="italic text-zinc-500 dark:text-zinc-400">
              Analysis and evidence: not produced (transport failure). Resume
              the run after fixing the underlying issue to retry this finding.
            </p>
          </div>
        ) : null}
        {!isPending && !isSkipped && !isFail ? (
          <>
            {detail.coder_analysis ? (
              <div>
                <h5 className="mb-0.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
                  Analysis
                </h5>
                <p className="whitespace-pre-wrap leading-relaxed">
                  {detail.coder_analysis}
                </p>
              </div>
            ) : null}
            {detail.coder_reason ? (
              <div>
                <h5 className="mb-0.5 text-[10px] font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
                  Reason
                </h5>
                <p className="whitespace-pre-wrap leading-relaxed">
                  {detail.coder_reason}
                </p>
              </div>
            ) : null}
            {detail.coder_call_chain_evidence.length > 0 ? (
              <div>
                <h5 className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
                  Call-chain evidence
                </h5>
                <div className="space-y-2">
                  {detail.coder_call_chain_evidence.map((evidence, idx) => (
                    <details
                      key={`${evidence.file_path}-${evidence.ordinal ?? idx}`}
                      className="overflow-hidden rounded border border-zinc-200 dark:border-zinc-800"
                    >
                      <summary className="cursor-pointer select-none bg-zinc-50 px-2 py-1 font-mono text-[11px] text-zinc-600 dark:bg-zinc-900 dark:text-zinc-300">
                        {evidence.file_path}
                        {evidence.function_name ? (
                          <span className="ml-2 text-zinc-400">
                            · {evidence.function_name}
                          </span>
                        ) : null}
                        <span className="ml-2 text-zinc-400">· {evidence.role}</span>
                      </summary>
                      <SyntaxHighlighter
                        language={evidence.language ?? "text"}
                        style={darkSyntax ? oneDark : oneLight}
                        customStyle={{
                          margin: 0,
                          fontSize: "12px",
                          padding: "10px",
                        }}
                        wrapLongLines
                      >
                        {evidence.snippet}
                      </SyntaxHighlighter>
                    </details>
                  ))}
                </div>
              </div>
            ) : null}
          </>
        ) : null}
      </div>
    </details>
  );
}
