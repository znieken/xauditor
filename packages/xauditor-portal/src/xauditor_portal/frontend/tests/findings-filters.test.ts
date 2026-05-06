import { describe, expect, it } from "vitest";
import {
  VALIDATION_DEFAULT,
  parseFiltersFromSearchParams,
  serializeFiltersToQueryString,
} from "@/lib/findings-filters";

describe("findings-filters URL plumbing", () => {
  it("parses repeated query params into arrays", () => {
    const sp = new URLSearchParams(
      "?validation_status=Valid&validation_status=Inconclusive&confidence=High&file=auth",
    );
    const filters = parseFiltersFromSearchParams(sp);
    expect(filters.validation_status).toEqual(["Valid", "Inconclusive"]);
    expect(filters.confidence).toEqual(["High"]);
    expect(filters.file).toBe("auth");
  });

  it("treats `?validation_status=` as an explicit empty array (FP-default suppressor)", () => {
    const sp = new URLSearchParams("?validation_status=");
    const filters = parseFiltersFromSearchParams(sp);
    // The key is present, so the page-level default-injection logic
    // honors the URL: validation_status is set, but to no values.
    expect(filters.validation_status).toEqual([]);
  });

  it("omits absent keys entirely so the page can apply the FP-default", () => {
    const sp = new URLSearchParams("?file=auth");
    const filters = parseFiltersFromSearchParams(sp);
    expect("validation_status" in filters).toBe(false);
  });

  it("serializes arrays as repeated key=value pairs", () => {
    const qs = serializeFiltersToQueryString({
      validation_status: ["Valid", "Partial Valid", "Inconclusive"],
      confidence: ["High", "Medium"],
      file: "auth",
    });
    const sp = new URLSearchParams(qs);
    expect(sp.getAll("validation_status")).toEqual([
      "Valid",
      "Partial Valid",
      "Inconclusive",
    ]);
    expect(sp.getAll("confidence")).toEqual(["High", "Medium"]);
    expect(sp.get("file")).toBe("auth");
  });

  it("emits the empty marker `?validation_status=` when the array is empty", () => {
    const qs = serializeFiltersToQueryString({ validation_status: [] });
    expect(qs).toBe("validation_status=");
  });

  it("omits other empty arrays entirely", () => {
    const qs = serializeFiltersToQueryString({
      confidence: [],
      exploitation_status: [],
      feedback_label: [],
    });
    expect(qs).toBe("");
  });

  it("round-trips the FP-excluding default", () => {
    const filters = { validation_status: [...VALIDATION_DEFAULT] };
    const qs = serializeFiltersToQueryString(filters);
    const reparsed = parseFiltersFromSearchParams(new URLSearchParams(qs));
    expect(reparsed.validation_status).toEqual([...VALIDATION_DEFAULT]);
  });
});
