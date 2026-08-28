/**
 * Prime Agent v0.8.1 extension: World Gateway investigation tools.
 *
 * Registers the nine approved investigation tools under identifier-safe
 * underscore aliases and maps each alias to its canonical dotted World Gateway
 * route in a single APPROVED_TOOLS mapping. Tool calls forward typed arguments
 * to WORLD_GATEWAY_URL/tools/{canonical_name} using a Bearer
 * WORLD_GATEWAY_TOKEN.
 *
 * Results are returned as typed text content. Failures throw sanitized Error
 * objects; the World Gateway token, control-plane secrets, process
 * environment, and raw HTTP headers are never exposed to the model.
 *
 * Discovery path (copied by the Modal image recipe):
 *   /root/.prime/agent/extensions/world_gateway.ts
 * Contract reference (pinned upstream):
 *   https://github.com/PrimeIntellect-ai/prime-agent/blob/v0.8.1/packages/coding-agent/docs/extensions.md
 */

import { Type } from "typebox";

/** Identifier-safe tool alias plus its canonical dotted World Gateway route. */
export interface ToolSpec {
  name: string;
  canonical: string;
  label: string;
  description: string;
  parameters: ReturnType<typeof Type.Object>;
}

export const APPROVED_TOOLS: ToolSpec[] = [
  {
    name: "trace_read",
    canonical: "trace.read",
    label: "Read trace",
    description:
      "Read trace telemetry, span details, and observed attributes for the incident under investigation.",
    parameters: Type.Object({
      trace_id: Type.Optional(Type.String({ description: "Trace identifier to inspect" })),
    }),
  },
  {
    name: "world_describe",
    canonical: "world.describe",
    label: "Describe world",
    description:
      "Describe the simulated reproduction environment, world metadata, and the active environment slice.",
    parameters: Type.Object({}),
  },
  {
    name: "environment_status",
    canonical: "environment.status",
    label: "Environment status",
    description:
      "Check the health, connectivity, and status of sandbox reproduction services.",
    parameters: Type.Object({}),
  },
  {
    name: "scenario_run",
    canonical: "scenario.run",
    label: "Run scenario",
    description:
      "Execute a reproduction scenario or test workload against the sandbox environment.",
    parameters: Type.Object({
      scenario: Type.String({ description: "Scenario identifier or command name to execute" }),
      parameters: Type.Optional(Type.Object({}, { description: "Optional execution parameters" })),
    }),
  },
  {
    name: "state_inspect",
    canonical: "state.inspect",
    label: "Inspect state",
    description:
      "Inspect runtime database tables, state stores, or service state inside the reproduction sandbox.",
    parameters: Type.Object({
      target: Type.Optional(Type.String({ description: "Target table, collection, or key path" })),
    }),
  },
  {
    name: "state_diff",
    canonical: "state.diff",
    label: "State diff",
    description:
      "Compute the state differential between the initial baseline and the current reproduction state.",
    parameters: Type.Object({}),
  },
  {
    name: "evidence_read",
    canonical: "evidence.read",
    label: "Read evidence",
    description:
      "Read captured evidence artifacts, reproduction logs, or diagnostic dumps.",
    parameters: Type.Object({
      ref: Type.Optional(Type.String({ description: "Evidence reference identifier" })),
    }),
  },
  {
    name: "proposal_create",
    canonical: "proposal.create",
    label: "Create proposal",
    description:
      "Create an investigation hypothesis or reproduction proposal with findings and hypothesis.",
    parameters: Type.Object({
      title: Type.String({ description: "Proposal title" }),
      hypothesis: Type.String({ description: "Root cause hypothesis description" }),
    }),
  },
  {
    name: "summary_submit",
    canonical: "summary.submit",
    label: "Submit summary",
    description:
      "Submit the final investigation summary with findings, recommended next step, and evidence refs.",
    parameters: Type.Object({
      findings: Type.String({ description: "Detailed root cause analysis and findings" }),
      next_step: Type.String({ description: "Recommended remediation or next operational step" }),
      evidence_refs: Type.Array(Type.String(), {
        description: "Evidence reference identifiers supporting the findings",
      }),
    }),
  },
];

function gatewayBaseUrl(): string {
  return (process.env.WORLD_GATEWAY_URL || "http://127.0.0.1:8001").replace(/\/+$/, "");
}

function gatewayToken(): string {
  return process.env.WORLD_GATEWAY_TOKEN || "";
}

/** Extract a short sanitized `detail` from an error body; never raw bytes. */
function errorDetailFromBody(raw: string): string {
  if (!raw) {
    return "";
  }
  try {
    const parsed = JSON.parse(raw) as { detail?: unknown };
    if (typeof parsed.detail === "string") {
      const detail = parsed.detail;
      return detail.length > 200 ? `${detail.slice(0, 200)}...` : detail;
    }
  } catch {
    // Non-JSON body: do not surface raw bytes to the model.
  }
  return "";
}

/** Bounded per-call wall clock for a gateway HTTP request (milliseconds). */
const WORLD_GATEWAY_FETCH_TIMEOUT_MS = 30_000;

/** POST typed arguments to the canonical gateway route with bearer auth. */
async function invokeGateway(
  url: string,
  token: string,
  canonical: string,
  args: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<unknown> {
  // Combine Prime's own cancellation signal with a bounded timeout so a hung
  // gateway call can never leave a tool execution running forever.
  const boundedSignal = signal
    ? AbortSignal.any([signal, AbortSignal.timeout(WORLD_GATEWAY_FETCH_TIMEOUT_MS)])
    : AbortSignal.timeout(WORLD_GATEWAY_FETCH_TIMEOUT_MS);
  const response = await fetch(`${url}/tools/${canonical}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ arguments: args || {} }),
    signal: boundedSignal,
  });

  if (!response.ok) {
    const raw = await response.text().catch(() => "");
    const detail = errorDetailFromBody(raw);
    const suffix = detail ? `: ${detail}` : "";
    throw new Error(
      `World Gateway tool ${canonical} returned HTTP ${response.status}${suffix}`,
    );
  }

  const data = (await response.json()) as { result?: unknown } | null;
  return data && data.result !== undefined ? data.result : data;
}

export default function registerWorldGatewayTools(pi: {
  registerTool: (tool: {
    name: string;
    label: string;
    description: string;
    parameters: unknown;
    execute: (
      toolCallId: string,
      params: Record<string, unknown>,
      signal?: AbortSignal,
    ) => Promise<{ content: Array<{ type: "text"; text: string }>; details: Record<string, unknown> }>;
  }) => void;
}): void {
  const url = gatewayBaseUrl();
  const token = gatewayToken();

  for (const tool of APPROVED_TOOLS) {
    pi.registerTool({
      name: tool.name,
      label: tool.label,
      description: tool.description,
      parameters: tool.parameters,
      execute: async (toolCallId, params, signal) => {
        if (signal?.aborted) {
          throw new Error(`World Gateway tool ${tool.canonical} was aborted`);
        }
        let result: unknown;
        try {
          result = await invokeGateway(url, token, tool.canonical, params || {}, signal);
        } catch (err) {
          const message = err instanceof Error ? err.message : String(err);
          throw new Error(`World Gateway tool ${tool.canonical} failed: ${message}`);
        }
        const text = typeof result === "string" ? result : JSON.stringify(result);
        return {
          content: [{ type: "text", text }],
          details: { tool: tool.canonical, toolCallId },
        };
      },
    });
  }
}