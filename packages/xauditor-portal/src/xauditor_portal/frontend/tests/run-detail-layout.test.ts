import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * Structural test for task 4.5 — the run-detail page must expose exactly
 * three sub-tabs (`Findings`, `Coverage`, `Audit Log`) and must no longer
 * reference the retired views.
 *
 * This is a source-level assertion instead of a render test because the
 * page uses Next's `useParams` + a chain of data hooks; rendering it in
 * isolation would require a deep mock stack that would not actually
 * enforce the layout. Re-read the file contents and check constants.
 */
describe("run-detail page sub-tab layout", () => {
  const pagePath = resolve(
    __dirname,
    "../app/(authenticated)/reports/runs/[runId]/page.tsx",
  );
  const source = readFileSync(pagePath, "utf-8");

  it("declares exactly three sub-tab ids", () => {
    const typeMatch = source.match(
      /type SubTab =\s*([^;]*);/,
    );
    expect(typeMatch).not.toBeNull();
    const typeLiteral = typeMatch![1].replace(/\s+/g, "");
    expect(typeLiteral).toBe('"findings"|"coverage"|"audit-log"');
  });

  it("lists the three sub-tabs in Findings / Coverage / Audit Log order", () => {
    const subTabsMatch = source.match(/const SUB_TABS[^=]*=\s*(\[[\s\S]*?\]);/);
    expect(subTabsMatch).not.toBeNull();
    const body = subTabsMatch![1];
    const ids = [...body.matchAll(/id:\s*"([^"]+)"/g)].map((m) => m[1]);
    expect(ids).toEqual(["findings", "coverage", "audit-log"]);
  });

  it("does not reference the retired Debates or Subagents views", () => {
    expect(source).not.toMatch(/DebatesView/);
    expect(source).not.toMatch(/SubagentsView/);
    expect(source).not.toMatch(/"debates"/);
    expect(source).not.toMatch(/"subagents"/);
    expect(source).not.toMatch(/"analyzer"/);
    expect(source).not.toMatch(/"validator"/);
    expect(source).not.toMatch(/"exploitation"/);
  });

  it("renders the overall audit progress percent unconditionally", () => {
    // The progress indicator block is outside the `running ? … : null`
    // branch; the "refreshing" caption is the only part still gated by
    // run-status.
    expect(source).toMatch(/Overall audit progress/);
    // The old code-path had the progress bar inside a `running ? …`
    // ternary — make sure that is gone.
    const matches = source.match(
      /running\s*\?\s*\(\s*<div[\s\S]*?<ProgressBar[\s\S]*?\/>/,
    );
    expect(matches).toBeNull();
  });
});
