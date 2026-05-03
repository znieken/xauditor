import { describe, expect, it } from "vitest";
// @ts-expect-error — next.config.mjs has no .d.ts, but it exports a default config object.
import nextConfig from "../next.config.mjs";

describe("next.config redirects for hidden Settings tab", () => {
  it("declares an async redirects() function", () => {
    expect(typeof nextConfig.redirects).toBe("function");
  });

  it("redirects /settings to /reports as a non-permanent (307) redirect", async () => {
    const rules = await nextConfig.redirects();
    const exact = rules.find(
      (r: { source: string }) => r.source === "/settings",
    );
    expect(exact).toBeDefined();
    expect(exact.destination).toBe("/reports");
    expect(exact.permanent).toBe(false);
  });

  it("redirects every /settings/:path* sub-route to /reports", async () => {
    const rules = await nextConfig.redirects();
    const wildcard = rules.find(
      (r: { source: string }) => r.source === "/settings/:path*",
    );
    expect(wildcard).toBeDefined();
    expect(wildcard.destination).toBe("/reports");
    expect(wildcard.permanent).toBe(false);
  });
});
