"use client";

import * as React from "react";
import { Badge } from "@/components/ui/badge";
import { useFindingDebate } from "@/lib/api";
import { ApiError } from "@/lib/api";
import type { DebateRound, FindingDebate } from "@/lib/types";

interface Props {
  runId: string;
  findingId: string;
}

export function FindingDebateSection({ runId, findingId }: Props) {
  const [open, setOpen] = React.useState(false);
  const { data, error, isLoading } = useFindingDebate(runId, findingId, open);

  // If the section is closed we render just the summary — no network cost.
  return (
    <details
      className="rounded-md border border-zinc-200 dark:border-zinc-800"
      open={open}
      onToggle={(event) => {
        const element = event.currentTarget as HTMLDetailsElement;
        setOpen(element.open);
      }}
    >
      <summary className="cursor-pointer select-none px-3 py-2 text-sm font-medium text-zinc-800 dark:text-zinc-200">
        Validator debate
        <span className="ml-2 text-xs font-normal text-zinc-500 dark:text-zinc-400">
          (click to expand)
        </span>
      </summary>
      <div className="border-t border-zinc-200 px-3 py-3 dark:border-zinc-800">
        {isLoading ? (
          <p className="text-xs text-zinc-500 dark:text-zinc-400">
            Loading debate…
          </p>
        ) : error instanceof ApiError && error.status === 404 ? (
          <p className="text-xs text-zinc-500 dark:text-zinc-400">
            No validator debate recorded for this finding.
          </p>
        ) : error ? (
          <p
            role="alert"
            className="rounded-md bg-rose-50 px-2 py-1 text-xs text-rose-700 dark:bg-rose-950 dark:text-rose-200"
          >
            Failed to load debate.
          </p>
        ) : data ? (
          <DebateBody debate={data} />
        ) : null}
      </div>
    </details>
  );
}

function DebateBody({ debate }: { debate: FindingDebate }) {
  return (
    <div className="space-y-3 text-sm">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge tone="info">Verdict: {debate.final_verdict}</Badge>
        <Badge tone="neutral">{debate.convergence_state}</Badge>
        <Badge tone="neutral">cap {debate.configured_round_cap}</Badge>
      </div>
      {debate.rounds.length === 0 ? (
        <p className="text-xs text-zinc-500 dark:text-zinc-400">
          No debate rounds were recorded.
        </p>
      ) : (
        debate.rounds.map((round) => (
          <DebateRoundBlock key={round.round_index} round={round} />
        ))
      )}
      <p className="text-[11px] text-zinc-500 dark:text-zinc-400">
        path fingerprint <span className="font-mono">{debate.path_fingerprint}</span>
      </p>
    </div>
  );
}

function DebateRoundBlock({ round }: { round: DebateRound }) {
  return (
    <section className="rounded-md border border-zinc-100 dark:border-zinc-800">
      <header className="border-b border-zinc-100 bg-zinc-50 px-2 py-1 text-xs font-semibold text-zinc-700 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-200">
        Round {round.round_index}
      </header>
      <div className="space-y-2 px-2 py-2">
        {round.turns.length === 0 ? (
          <p className="text-xs text-zinc-500 dark:text-zinc-400">
            No subagent turns in this round.
          </p>
        ) : (
          round.turns.map((turn, idx) => (
            <div
              key={`${turn.subagent_index}-${idx}`}
              className="rounded-md bg-zinc-50 px-2 py-2 dark:bg-zinc-900/40"
            >
              <div className="flex flex-wrap items-center gap-2 text-xs text-zinc-700 dark:text-zinc-200">
                <span className="font-mono">
                  subagent-{turn.subagent_index ?? "?"}
                </span>
                {turn.provider_name ? (
                  <Badge tone="neutral">{turn.provider_name}</Badge>
                ) : null}
                {turn.verdict ? (
                  <Badge tone="info">verdict: {turn.verdict}</Badge>
                ) : null}
              </div>
              {turn.rebuttal ? (
                <p className="mt-1 whitespace-pre-wrap text-xs text-zinc-700 dark:text-zinc-300">
                  <span className="font-semibold">Rebuttal:</span> {turn.rebuttal}
                </p>
              ) : null}
              {turn.raw_response ? (
                <pre className="mt-1 max-h-40 overflow-auto rounded bg-white p-1.5 text-[11px] leading-relaxed dark:bg-zinc-950/60">
                  {turn.raw_response}
                </pre>
              ) : null}
            </div>
          ))
        )}
      </div>
    </section>
  );
}
