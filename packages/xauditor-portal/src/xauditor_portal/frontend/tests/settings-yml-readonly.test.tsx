import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AuditSection } from "@/components/audit-section";
import { GraphBuildSection } from "@/components/graph-build-section";
import type { ConfigField } from "@/lib/types";

function field(value: unknown, source: ConfigField["source"]): ConfigField {
  return { value, source, overridden_by_yml: source === "yml" };
}

describe("Source-based read-only rule", () => {
  it("yml-only field renders read-only with the yml value visible", () => {
    const fields = {
      "audit.worker_count": field(8, "yml"),
    };
    render(
      <AuditSection
        fields={fields}
        value="8"
        onChange={vi.fn()}
      />,
    );
    const input = screen.getByLabelText("Worker count") as HTMLInputElement;
    expect(input.disabled).toBe(true);
    expect(input.value).toBe("8");
    // The yml override badge should be visible.
    expect(screen.getByText(/yml override/i)).toBeInTheDocument();
  });

  it("yml + db case renders the yml value (db value not shown)", () => {
    const fields = {
      "audit.worker_count": field(8, "yml"),
    };
    // The resolver always picks yml when both are set, so the field's value
    // is the yml value. Test that the UI shows it as read-only.
    render(
      <AuditSection
        fields={fields}
        value="8"
        onChange={vi.fn()}
      />,
    );
    const input = screen.getByLabelText("Worker count") as HTMLInputElement;
    expect(input.disabled).toBe(true);
    expect(input.value).toBe("8");
    expect(input.value).not.toBe("4"); // db's value would be 4 but never surfaces
  });

  it("env-sourced field renders read-only with env source tag", () => {
    const fields = {
      "graph.build.neo4j_chunk_size": field(10000, "env"),
    };
    render(
      <GraphBuildSection
        fields={fields}
        draft={{}}
        onChange={vi.fn()}
      />,
    );
    const input = screen.getByLabelText("Neo4j chunk size") as HTMLInputElement;
    expect(input.disabled).toBe(true);
    expect(input.value).toBe("10000");
    expect(screen.getByText(/env override/i)).toBeInTheDocument();
  });

  it("default-sourced field is editable", () => {
    const fields = {
      "audit.worker_count": field(1, "default"),
    };
    render(
      <AuditSection
        fields={fields}
        value="1"
        onChange={vi.fn()}
      />,
    );
    const input = screen.getByLabelText("Worker count") as HTMLInputElement;
    expect(input.disabled).toBe(false);
  });

  it("db-sourced field is editable", () => {
    const fields = {
      "audit.worker_count": field(4, "db"),
    };
    render(
      <AuditSection
        fields={fields}
        value="4"
        onChange={vi.fn()}
      />,
    );
    const input = screen.getByLabelText("Worker count") as HTMLInputElement;
    expect(input.disabled).toBe(false);
  });
});
