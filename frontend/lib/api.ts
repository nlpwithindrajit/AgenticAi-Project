import type { TravelRequest, TripPlan } from "./types";

/**
 * Where the FastAPI backend lives.
 *
 * Read at module scope from a NEXT_PUBLIC_ variable so it is baked into the
 * client bundle at build time — which means the Docker image is environment
 * specific, and the deploy script passes it as a build arg.
 */
export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ??
  "http://127.0.0.1:8000";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** POST /plan-trip. Surfaces the backend's own error detail where it has one. */
export async function planTrip(request: TravelRequest): Promise<TripPlan> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/plan-trip`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    });
  } catch {
    throw new ApiError(
      `Could not reach the planner at ${API_BASE}. Is the API running?`,
      0,
    );
  }

  if (!response.ok) {
    throw new ApiError(await describeFailure(response), response.status);
  }
  return (await response.json()) as TripPlan;
}

async function describeFailure(response: Response): Promise<string> {
  try {
    const body = await response.json();
    if (typeof body?.detail === "string") return body.detail;
    // FastAPI validation errors arrive as a list of field-level problems.
    if (Array.isArray(body?.detail)) {
      return body.detail
        .map((d: { loc?: unknown[]; msg?: string }) => {
          const field = Array.isArray(d.loc) ? d.loc.slice(1).join(".") : "";
          return field ? `${field}: ${d.msg}` : d.msg;
        })
        .join("; ");
    }
  } catch {
    // fall through to the status line
  }
  return `The planner returned ${response.status}.`;
}

export async function checkHealth(): Promise<{
  status: string;
  environment: string;
  langfuse_enabled: boolean;
} | null> {
  try {
    const response = await fetch(`${API_BASE}/health`, { cache: "no-store" });
    if (!response.ok) return null;
    return await response.json();
  } catch {
    return null;
  }
}
