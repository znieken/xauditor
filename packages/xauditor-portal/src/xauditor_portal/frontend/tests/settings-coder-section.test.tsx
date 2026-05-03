import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { CoderSection, isLocalEndpoint } from "@/components/coder-section";
import type { ConfigField, RedactedKey } from "@/lib/types";

function field(value: unknown, source: ConfigField["source"] = "default"): ConfigField {
  return { value, source, overridden_by_yml: source === "yml" };
}

function defaultFields(transport = "subprocess"): Record<string, ConfigField> {
  return {
    "coder.transport": field(transport),
  };
}

describe("isLocalEndpoint", () => {
  it("treats empty endpoints as local", () => {
    expect(isLocalEndpoint("")).toBe(true);
  });
  it("treats unix:// endpoints as local", () => {
    expect(isLocalEndpoint("unix:///var/run/coder.sock")).toBe(true);
  });
  it("treats loopback IPs and localhost as local", () => {
    expect(isLocalEndpoint("http://localhost:8001")).toBe(true);
    expect(isLocalEndpoint("http://127.0.0.1:8001")).toBe(true);
    expect(isLocalEndpoint("http://[::1]:8001")).toBe(true);
  });
  it("treats remote hosts as not-local", () => {
    expect(isLocalEndpoint("http://coder.internal:8001")).toBe(false);
    expect(isLocalEndpoint("https://example.com")).toBe(false);
  });
});

describe("CoderSection", () => {
  it("renders every editable field except deprecated repo_mount_path", () => {
    const onChange = vi.fn();
    render(
      <CoderSection
        fields={defaultFields()}
        redactedKeys={{}}
        draft={{}}
        onChange={onChange}
      />,
    );
    for (const label of [
      "Enabled",
      "Transport",
      "CLI command",
      "Concurrency",
      "Thinking effort",
      "Model URL",
      "Model name",
      "Model API key",
      "Request timeout (s)",
      "Working directory",
      "Endpoint",
      "Enable auth",
      "Endpoint token",
      "Poll interval (s)",
      "Preflight timeout (s)",
      "Container image",
      "Container name",
      "Runtime socket path",
      "Workspace root",
      "Project name",
    ]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    // No control labelled "Repo mount path" — deprecated.
    expect(screen.queryByText(/Repo mount path/i)).toBeNull();
  });

  it("disables HTTP-only fields when transport is subprocess", () => {
    render(
      <CoderSection
        fields={defaultFields("subprocess")}
        redactedKeys={{}}
        draft={{}}
        onChange={vi.fn()}
      />,
    );
    expect(
      (screen.getByLabelText("Endpoint") as HTMLInputElement).disabled,
    ).toBe(true);
    expect(
      (screen.getByLabelText("Enable auth") as HTMLSelectElement).disabled,
    ).toBe(true);
    expect(
      (screen.getByLabelText("Container image") as HTMLInputElement).disabled,
    ).toBe(true);
  });

  it("enables HTTP-only fields when transport is http with a local endpoint", () => {
    const fields = {
      ...defaultFields("http"),
      "coder.endpoint": field("http://localhost:8001"),
    };
    render(
      <CoderSection
        fields={fields}
        redactedKeys={{}}
        draft={{ transport: "http", endpoint: "http://localhost:8001" }}
        onChange={vi.fn()}
      />,
    );
    expect(
      (screen.getByLabelText("Endpoint") as HTMLInputElement).disabled,
    ).toBe(false);
    expect(
      (screen.getByLabelText("Container image") as HTMLInputElement).disabled,
    ).toBe(false);
  });

  it("disables container-only fields when endpoint is remote", () => {
    render(
      <CoderSection
        fields={defaultFields("http")}
        redactedKeys={{}}
        draft={{ transport: "http", endpoint: "https://coder.internal" }}
        onChange={vi.fn()}
      />,
    );
    expect(
      (screen.getByLabelText("Endpoint") as HTMLInputElement).disabled,
    ).toBe(false); // endpoint stays editable
    expect(
      (screen.getByLabelText("Container image") as HTMLInputElement).disabled,
    ).toBe(true);
    expect(
      (screen.getByLabelText("Workspace root") as HTMLInputElement).disabled,
    ).toBe(true);
  });

  it("renders cli_command via ChipInput and emits chip arrays", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <CoderSection
        fields={defaultFields()}
        redactedKeys={{}}
        draft={{}}
        onChange={onChange}
      />,
    );
    const cliInput = screen.getByLabelText("CLI command") as HTMLInputElement;
    await user.click(cliInput);
    await user.keyboard("claude{Enter}");
    expect(onChange).toHaveBeenCalledWith({ cli_command: ["claude"] });
  });

  it("thinking_effort offers an (unset) option that submits empty", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <CoderSection
        fields={defaultFields()}
        redactedKeys={{}}
        draft={{ thinking_effort: "max" }}
        onChange={onChange}
      />,
    );
    const select = screen.getByLabelText("Thinking effort") as HTMLSelectElement;
    await user.selectOptions(select, "");
    expect(onChange).toHaveBeenCalledWith({ thinking_effort: "" });
  });

  it("renders secret stubs based on redactedKeys", () => {
    const redactedKeys: Record<string, RedactedKey> = {
      "coder.model_api_key": { present: true, source: "yml" },
      "coder.endpoint_token": { present: false, source: "default" },
    };
    render(
      <CoderSection
        fields={defaultFields()}
        redactedKeys={redactedKeys}
        draft={{}}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByText("configured")).toBeInTheDocument();
    expect(screen.getByText("not configured")).toBeInTheDocument();
  });
});
