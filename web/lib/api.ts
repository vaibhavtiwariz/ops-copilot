export type AgentEvent =
  | { type: "connected"; thread_id: string }
  | { type: "run_started" }
  | { type: "node"; nodes: string[] }
  | { type: "token"; node?: string; content: string }
  | { type: "interrupt"; id?: string; payload?: ApprovalPayload; interrupts?: { id: string; payload: ApprovalPayload }[] }
  | { type: "done" }
  | { type: "error"; message: string; detail?: string }
  | { type: "rate_limited"; wait_seconds: number; context?: string }
  | { type: "pong" };

export interface ApprovalPayload {
  kind: "db_write" | "github_comment";
  proposal_id?: number;
  action?: string;
  table?: string;
  rationale?: string;
  repo?: string;
  number?: number;
  body?: string;
  summary: string;
}

// S6: the interrupt payload no longer carries the SQL. The reviewer fetches
// it on demand through this endpoint; it is never in checkpoints, audit
// records, or /state.
export interface ProposalView {
  proposal_id: number;
  action: string;
  table: string;
  sql: string;
  status: string;
}

export interface TaskView {
  id: string;
  description: string;
  assignee: string;
  status: string;
  summary: string;
}

export interface ChatMessage {
  role: "user" | "assistant" | "system";
  content: string;
}

export const API_URL =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// Module-level key, set from the console's API key field. Sent as X-API-Key
// on every fetch so the console keeps working once OPS_API_KEY is required
// server-side. Memory only: never logged or persisted.
let apiKey = "";

export function setApiKey(k: string): void {
  apiKey = k;
}

// Subprotocols for the WebSocket handshake: browsers cannot set headers
// there, so the key travels as an "x-api-key.<key>" protocol token and the
// thread token as "x-thread-token.<tok>". The server closes the socket with
// 4401/4403 when either is missing or wrong.
export function wsSubprotocols(threadId?: string): string[] {
  const protos = ["copilot"];
  if (apiKey) protos.push("x-api-key." + apiKey);
  const tok = threadId ? threadTokens.get(threadId) : undefined;
  if (tok) protos.push("x-thread-token." + tok);
  return protos;
}

function withAuth(extra: Record<string, string> = {}): Record<string, string> {
  return apiKey ? { ...extra, "X-API-Key": apiKey } : extra;
}

// S4: per-thread ownership tokens, kept in memory keyed by thread id, next
// to the API key. Never logged or persisted.
const threadTokens = new Map<string, string>();

export function setThreadToken(threadId: string, token: string): void {
  threadTokens.set(threadId, token);
}

function threadHeaders(
  threadId: string,
  extra: Record<string, string> = {},
): Record<string, string> {
  const headers = withAuth(extra);
  const tok = threadTokens.get(threadId);
  if (tok) headers["X-Thread-Token"] = tok;
  return headers;
}

export interface ThreadHandle {
  threadId: string;
  threadToken: string;
}

export interface AuditRecord {
  ts: string;
  kind: string;
  actor: string;
  summary: string;
  detail: Record<string, unknown>;
}

export interface ThreadListItem {
  thread_id: string;
  created_at: string | null;
  status: string;
  message_count: number;
}

export async function listThreads(): Promise<ThreadListItem[]> {
  const res = await fetch(`${API_URL}/api/threads`, { headers: withAuth() });
  if (!res.ok) throw new Error(`thread list failed (${res.status})`);
  return (await res.json()) as ThreadListItem[];
}

export function hasThreadToken(threadId: string): boolean {
  return threadTokens.has(threadId);
}

export async function createThread(): Promise<ThreadHandle> {
  const res = await fetch(`${API_URL}/api/threads`, {
    method: "POST",
    headers: withAuth(),
  });
  if (!res.ok) throw new Error(`thread creation failed (${res.status})`);
  const data = await res.json();
  const handle: ThreadHandle = {
    threadId: data.thread_id as string,
    threadToken: data.thread_token as string,
  };
  setThreadToken(handle.threadId, handle.threadToken);
  return handle;
}

export async function getThreadState(
  threadId: string,
): Promise<{ tasks: TaskView[]; awaiting: ApprovalPayload | null }> {
  const res = await fetch(`${API_URL}/api/threads/${threadId}/state`, {
    headers: threadHeaders(threadId),
  });
  if (!res.ok) throw new Error(`state fetch failed (${res.status})`);
  return (await res.json()) as { tasks: TaskView[]; awaiting: ApprovalPayload | null };
}

export async function getTranscript(threadId: string): Promise<ChatMessage[]> {
  const res = await fetch(`${API_URL}/api/threads/${threadId}/transcript`, {
    headers: threadHeaders(threadId),
  });
  if (!res.ok) throw new Error(`transcript fetch failed (${res.status})`);
  return (await res.json()) as ChatMessage[];
}

export async function getThreadAudit(threadId: string): Promise<AuditRecord[]> {
  const res = await fetch(`${API_URL}/api/threads/${threadId}/audit`, {
    headers: threadHeaders(threadId),
  });
  if (!res.ok) throw new Error(`audit fetch failed (${res.status})`);
  return (await res.json()) as AuditRecord[];
}

export async function getProposal(
  threadId: string,
  proposalId: number,
): Promise<ProposalView> {
  const res = await fetch(
    `${API_URL}/api/threads/${threadId}/proposals/${proposalId}`,
    { headers: threadHeaders(threadId) },
  );
  if (!res.ok) throw new Error(`proposal fetch failed (${res.status})`);
  return (await res.json()) as ProposalView;
}

export class ApprovalError extends Error {
  status: number;
  constructor(status: number, detail: string) {
    super(`approval failed (${status})${detail ? `: ${detail}` : ""}`);
    this.name = "ApprovalError";
    this.status = status;
  }
}

export async function submitApproval(
  threadId: string,
  approved: boolean,
  note = "",
): Promise<boolean> {
  const res = await fetch(`${API_URL}/api/threads/${threadId}/approvals`, {
    method: "POST",
    headers: threadHeaders(threadId, { "Content-Type": "application/json" }),
    body: JSON.stringify({ approved, note, decided_by: "ops-reviewer" }),
  });
  if (!res.ok) {
    // The decision did not go through (504 drain timeout, 409 busy, 502).
    // The caller branches on status: 409 means the gate is gone, every
    // other error restores the pending card for a re-decide.
    let detail = "";
    try {
      detail = (await res.json()).detail ?? "";
    } catch {
      detail = "";
    }
    throw new ApprovalError(res.status, detail);
  }
  return true;
}
