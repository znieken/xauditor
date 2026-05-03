export type SamplingField =
  | "temperature"
  | "top_p"
  | "top_k"
  | "repetition_penalty";

export interface SamplingValidationError {
  key: string;
  message: string;
}

/** Mirror of the CLI range rules used by the backend PATCH handler. */
export function validateSamplingValue(
  field: SamplingField,
  value: number,
): string | null {
  if (!Number.isFinite(value)) {
    return `${field} must be a number`;
  }
  if (field === "temperature" && value < 0) {
    return "temperature must be >= 0";
  }
  if (field === "top_p" && !(value > 0 && value <= 1)) {
    return "top_p must satisfy 0 < top_p <= 1";
  }
  if (field === "top_k" && (!Number.isInteger(value) || value < 1)) {
    return "top_k must be an integer >= 1";
  }
  if (field === "repetition_penalty" && value <= 0) {
    return "repetition_penalty must be > 0";
  }
  return null;
}

export function validateSamplingMap(
  values: Record<string, unknown>,
): SamplingValidationError[] {
  const errors: SamplingValidationError[] = [];
  for (const [key, raw] of Object.entries(values)) {
    if (raw === null || raw === undefined || raw === "") continue;
    const parts = key.split(".");
    const last = parts[parts.length - 1];
    if (!["temperature", "top_p", "top_k", "repetition_penalty"].includes(last)) {
      continue;
    }
    const parsed = typeof raw === "number" ? raw : Number(raw);
    const message = validateSamplingValue(last as SamplingField, parsed);
    if (message) {
      errors.push({ key, message });
    }
  }
  return errors;
}
