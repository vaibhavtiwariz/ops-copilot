"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  AgentEvent,
  ApprovalError,
  ApprovalPayload,
  TaskView,
  ThreadListItem,
  API_URL,
  createThread,
  getProposal,
  getThreadAudit,
  getThreadState,
  getTranscript,
  hasThreadToken,
  listThreads,
  setApiKey as setConsoleApiKey,
  submitApproval,
  wsSubprotocols,
} from "@/lib/api";

const WORKER_COLORS: Record<string, string> = {
  supervisor: "bg-slate-500",
  analyst: "bg-emerald-500",
  devops: "bg-sky-500",
  triage: "bg-amber-500",
  approval_gate: "bg-rose-500",
  executor: "bg-rose-400",
};

type Phase = "idle" | "connecting" | "running" | "awaiting_approval";

interface Bubble {
  role: "user" | "assistant" | "system";
  content: string;
}

export default function Home() {
  const [threadId, setThreadId] = useState<string | null>(null);
  const [bubbles, setBubbles] = useState<Bubble[]>([]);
  const [tasks, setTasks] = useState<TaskView[]>([]);
  const [activeNodes, setActiveNodes] = useState<string[]>([]);
  const [pending, setPending] = useState<ApprovalPayload | null>(null);
  // Resolved approvals stay visible after the fact: the card clears on
  // decide, but the decision itself is history worth keeping in the view.
  // Entries come from the thread audit (authoritative, survives thread
  // switches) plus an optimistic entry on decide; the audit fetch on
  // refresh replaces the list.
  const [resolvedApprovals, setResolvedApprovals] = useState<
    {
      summary: string;
      kind: string;
      approved: boolean;
      ts?: string;
      proposalId?: number;
      decidedBy?: string;
    }[]
  >([]);
  // Completed threads for the history panel. A thread lists only after
  // its work finished; reopening one in this session uses its token
  // from console memory, while after a reload it shows as locked.
  const [threadList, setThreadList] = useState<ThreadListItem[]>([]);
  const [lockedThread, setLockedThread] = useState<ThreadListItem | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [input, setInput] = useState("");
  // Kept in React state only: never logged, never persisted.
  const [apiKey, setApiKeyState] = useState("");
  const socketRef = useRef<WebSocket | null>(null);
  // The connect fail timer, so switching threads clears the previous one
  // instead of letting it post a stale "could not reach" bubble.
  const failTimerRef = useRef<number | null>(null);
  // refreshState runs inside the mount-time handleEvent closure, so it
  // cannot read state directly: threadId would stay null forever and a
  // finished run would never flip the UI back to idle. A ref carries the
  // current value into that closure.
  const threadIdRef = useRef<string | null>(null);
  // Tokens arrive in fragments tied to whichever model is speaking; a fresh
  // assistant bubble starts whenever a non-token frame lands in between.
  const openBubble = useRef(false);

  const handleEvent = useCallback((event: AgentEvent) => {
    switch (event.type) {
      case "run_started":
        setPhase("running");
        setPending(null);
        openBubble.current = false;
        break;
      case "node":
        setActiveNodes(event.nodes);
        openBubble.current = false; // node boundary -> next tokens start a new bubble
        break;
      case "token":
        appendAssistantText(event.content);
        break;
      case "interrupt": {
        const payload =
          event.payload ??
          event.interrupts?.[event.interrupts.length - 1]?.payload ??
          null;
        if (payload) {
          setPending(payload);
          setPhase("awaiting_approval");
        }
        break;
      }
      case "done":
        refreshState();
        break;
      case "rate_limited":
        // A silent "working..." reads as stalled; name the wait.
        setBubbles((prev) => [
          ...prev,
          {
            role: "system",
            content: `rate limited, waiting ~${event.wait_seconds}s before retrying${event.context ? ` (${event.context})` : ""}…`,
          },
        ]);
        break;
      case "error":
        setBubbles((prev) => [
          ...prev,
          { role: "system", content: `error: ${event.message}` },
        ]);
        setPhase("idle");
        break;
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  function appendAssistantText(text: string) {
    setBubbles((prev) => {
      const last = prev[prev.length - 1];
      if (last?.role === "assistant" && openBubble.current) {
        return [...prev.slice(0, -1), { role: "assistant", content: last.content + text }];
      }
      openBubble.current = true;
      return [...prev, { role: "assistant", content: text }];
    });
  }

  async function refreshThreads() {
    try {
      setThreadList(await listThreads());
    } catch {
      // history is best-effort; the live thread still works without it
    }
  }

  // First load shows history even before any thread starts.
  useEffect(() => {
    refreshThreads();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function refreshState() {
    const id = threadIdRef.current;
    if (!id) return;
    try {
      const data = await getThreadState(id);
      setTasks(data.tasks ?? []);
      if (data.awaiting) {
        setPending(data.awaiting);
        setPhase("awaiting_approval");
      } else {
        setPending(null);
        setActiveNodes([]);
        setPhase("idle");
      }
    } catch {
      setPhase("idle"); // state refresh is best-effort
    }
    // Transcript backstop: on open this restores the real conversation
    // for a reopened thread; on done it lands any answer whose WS tokens
    // were missed. Authoritative, so it replaces the streamed bubbles.
    try {
      setBubbles(await getTranscript(id));
    } catch {
      // transcript is best-effort; streamed bubbles still stand
    }
    // Resolved approvals from the audit: authoritative, survives thread
    // switches (unlike session-only state), carries timestamp, proposal
    // id, and who decided.
    try {
      const records = await getThreadAudit(id);
      setResolvedApprovals(
        records
          .filter((r) => r.kind === "approval" || r.kind === "rejection")
          .map((r) => {
            const detail = r.detail as Record<string, unknown>;
            const payload = detail.payload as { kind?: string; proposal_id?: number } | undefined;
            const proposalId =
              typeof detail.proposal_id === "number"
                ? detail.proposal_id
                : typeof payload?.proposal_id === "number"
                  ? payload.proposal_id
                  : undefined;
            return {
              summary: r.summary,
              kind: payload?.kind ?? (proposalId != null ? "db_write" : "unknown"),
              approved: r.kind === "approval",
              ts: r.ts,
              proposalId,
              decidedBy: r.actor,
            };
          }),
      );
    } catch {
      // audit history is best-effort; optimistic entries still stand
    }
    // A finished run may have just added this thread to history.
    refreshThreads();
  }

  // Connect the console socket to a thread id, new or existing. Switching
  // to an existing thread reuses its token from console memory; the
  // transcript reloads from the server (refreshState), so reopening
  // shows the real conversation, not an empty transcript.
  function connectThread(id: string) {
    setPhase("connecting");
    socketRef.current?.close();
    const ws = new WebSocket(
      `${API_URL.replace(/^http/, "ws")}/ws/${id}`,
      wsSubprotocols(id),
    );
    socketRef.current = ws;
    if (failTimerRef.current) window.clearTimeout(failTimerRef.current);
    const failTimer = window.setTimeout(() => {
      setBubbles((prev) => [
        ...prev,
        { role: "system", content: "error: could not reach the API websocket, is the backend up?" },
      ]);
      setPhase("idle");
    }, 8000);
    failTimerRef.current = failTimer;
    ws.onopen = () => {
      window.clearTimeout(failTimer);
      threadIdRef.current = id;
      setThreadId(id);
      setBubbles([]);
      setTasks([]);
      setPending(null);
      setResolvedApprovals([]);
      setActiveNodes([]);
      setLockedThread(null);
      refreshState();
    };
    ws.onmessage = (evt) => handleEvent(JSON.parse(evt.data) as AgentEvent);
    ws.onerror = () => {
      window.clearTimeout(failTimer);
      setBubbles((prev) => [
        ...prev,
        { role: "system", content: `error: websocket to ${API_URL} failed: check OPS_CORS_ORIGINS and that port 8000 is reachable` },
      ]);
      setPhase("idle");
    };
  }

  async function startThread() {
    setPhase("connecting");
    try {
      const { threadId: id } = await createThread();
      connectThread(id);
      refreshThreads();
    } catch (err) {
      setBubbles((prev) => [
        ...prev,
        { role: "system", content: `error: could not create a thread (${String(err).slice(0, 120)})` },
      ]);
      setPhase("idle");
    }
  }

  // Open a listed thread: fully in this session (token in memory), or show
  // the locked card after a reload, when the token is gone and the content
  // stays locked behind it.
  function selectThread(item: ThreadListItem) {
    if (hasThreadToken(item.thread_id)) {
      setLockedThread(null);
      connectThread(item.thread_id);
      refreshThreads();
    } else {
      setLockedThread(item);
    }
  }

  // The key itself lives in the api module; startThread reads it at call
  // time, so this state only drives the input field.

  function handleApiKeyChange(value: string) {
    setApiKeyState(value);
    setConsoleApiKey(value);
  }

  function send() {
    const content = input.trim();
    if (!content || !threadId || phase === "running") return;
    setMessages(content);
  }

  function setMessages(content: string) {
    setBubbles((prev) => [...prev, { role: "user", content }]);
    setInput("");
    socketRef.current?.send(JSON.stringify({ type: "user_message", content }));
  }

  async function decide(approved: boolean) {
    if (!threadId || !pending) return;
    const resolved = pending;
    setPending(null);
    setPhase("running");
    try {
      await submitApproval(threadId, approved, approved ? "" : "declined from console");
    } catch (err) {
      // A 409 means the gate is gone: the interrupt was already consumed
      // (e.g. a 504-cancel landed past the gate), so the card is stale.
      // Clear it and refresh instead of restoring a card that can never
      // decide. Known limitation: the run itself is dead (interrupt
      // consumed, no resume endpoint), so re-decide cannot revive it;
      // the honest UI is a cleared card, a refreshed state, and a new
      // thread. The 202 decoupling is the complete fix, a follow-up.
      if (err instanceof ApprovalError && err.status === 409) {
        setPending(null);
        setBubbles((prev) => [
          ...prev,
          {
            role: "system",
            content: "approval no longer pending: the gate already moved on, state refreshed",
          },
        ]);
        await refreshState();
        return;
      }
      // Every other error (504 drain timeout, 502, network): restore the
      // card so the user can re-decide, and say why.
      setPending(resolved);
      setPhase("awaiting_approval");
      setBubbles((prev) => [
        ...prev,
        { role: "system", content: `error: ${String(err).slice(0, 200)}` },
      ]);
      return;
    }
    setResolvedApprovals((prev) => [
      ...prev,
      { summary: resolved.summary, kind: resolved.kind, approved },
    ]);
  }

  return (
    <main className="flex h-screen bg-ink-950 text-slate-200">
      <Sidebar
        threadId={threadId}
        phase={phase}
        tasks={tasks}
        activeNodes={activeNodes}
        apiKey={apiKey}
        onApiKeyChange={handleApiKeyChange}
        onStart={startThread}
        threadList={threadList}
        onSelectThread={selectThread}
      />

      <section className="flex flex-1 flex-col">
        {lockedThread ? (
          <div className="flex flex-1 items-center justify-center">
            <div className="w-full max-w-2xl rounded-xl border border-ink-700 bg-ink-800 px-4 py-3 text-sm text-slate-300">
              <p className="font-medium text-slate-200">
                Thread {lockedThread.thread_id} is locked
              </p>
              <p className="mt-1 text-xs text-slate-400">
                {lockedThread.created_at
                  ? new Date(lockedThread.created_at).toLocaleString()
                  : "unknown time"}
                {" · "}
                {lockedThread.status === "rejected" ? "rejected" : "done"}
                {" · "}
                {lockedThread.message_count} messages
              </p>
              <p className="mt-2 text-sm">
                Chat content stays locked without this thread's token, which lives
                only in the browser session that created the thread. Start a new
                thread to continue.
              </p>
              <button
                onClick={() => setLockedThread(null)}
                className="mt-3 rounded-md border border-ink-700 px-4 py-1.5 text-sm text-slate-300 hover:bg-ink-800"
              >
                Back to current thread
              </button>
            </div>
          </div>
        ) : !threadId ? (
          <div className="flex flex-1 items-center justify-center">
            <button
              onClick={startThread}
              className="rounded-lg border border-ink-700 bg-ink-800 px-6 py-4 text-sm text-slate-300 transition hover:border-emerald-600/50 hover:text-emerald-300"
            >
              Start an operations session →
            </button>
          </div>
        ) : (
          <>
            <header className="flex items-center gap-3 border-b border-ink-700 px-6 py-3">
              <span className="font-mono text-xs text-slate-500">thread {threadId}</span>
              <span
                className={`ml-auto rounded-full px-2.5 py-0.5 text-xs font-medium ${
                  phase === "running"
                    ? "bg-sky-900/50 text-sky-300"
                    : phase === "awaiting_approval"
                      ? "bg-rose-900/50 text-rose-300"
                      : "bg-ink-800 text-slate-400"
                }`}
              >
                {phase === "running"
                  ? "working…"
                  : phase === "awaiting_approval"
                    ? "awaiting your review"
                    : "ready"}
              </span>
            </header>

            <div className="flex-1 space-y-4 overflow-y-auto px-6 py-6">
              {bubbles.map((b, i) => (
                <MessageBubble key={i} bubble={b} />
              ))}
              {pending && threadId && (
                <ApprovalCard threadId={threadId} payload={pending} onDecide={decide} />
              )}
              {resolvedApprovals.length > 0 && (
                <div className="mx-auto w-full max-w-2xl space-y-2">
                  <p className="text-xs font-semibold uppercase tracking-widest text-slate-500">
                    resolved approvals
                  </p>
                  {resolvedApprovals.map((r, i) => (
                    <div
                      key={i}
                      className="rounded-xl border border-ink-700 bg-ink-800 px-4 py-3 text-sm text-slate-300"
                    >
                      <span
                        className={
                          r.approved ? "font-medium text-emerald-300" : "font-medium text-rose-300"
                        }
                      >
                        {r.approved ? "Approved" : "Rejected"}
                      </span>{" "}
                      <span className="text-slate-500">({r.kind})</span> {r.summary}
                      <span className="block text-xs text-slate-500">
                        {r.ts ? new Date(r.ts).toLocaleString() : ""}
                        {r.proposalId != null ? ` · proposal ${r.proposalId}` : ""}
                        {r.decidedBy ? ` · by ${r.decidedBy}` : ""}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <footer className="border-t border-ink-700 p-4">
              <div className="flex gap-2">
                <input
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && send()}
                  disabled={phase === "running" || !threadId}
                  placeholder={
                    phase === "running"
                      ? "agents are working…"
                      : "ask about revenue, incidents, repos, or request a change"
                  }
                  className="flex-1 rounded-md border border-ink-700 bg-ink-900 px-4 py-2.5 text-sm outline-none placeholder:text-slate-600 focus:border-emerald-600/50 disabled:opacity-50"
                />
                <button
                  onClick={send}
                  disabled={phase === "running" || !input.trim()}
                  className="rounded-md bg-emerald-600 px-5 py-2.5 text-sm font-medium text-white transition hover:bg-emerald-500 disabled:opacity-30"
                >
                  send
                </button>
              </div>
            </footer>
          </>
        )}
      </section>
    </main>
  );
}

function Sidebar({
  threadId,
  phase,
  tasks,
  activeNodes,
  apiKey,
  onApiKeyChange,
  onStart,
  threadList,
  onSelectThread,
}: {
  threadId: string | null;
  phase: Phase;
  tasks: TaskView[];
  activeNodes: string[];
  apiKey: string;
  onApiKeyChange: (value: string) => void;
  onStart: () => void;
  threadList: ThreadListItem[];
  onSelectThread: (item: ThreadListItem) => void;
}) {
  return (
    <aside className="hidden w-72 flex-col border-r border-ink-700 bg-ink-900 p-4 md:flex">
      <h1 className="font-mono text-lg font-semibold tracking-tight text-white">
        ops<span className="text-emerald-400">·</span>copilot
      </h1>
      <p className="mt-1 text-xs text-slate-500">supervised agents · MCP tools · human gates</p>

      <label className="mt-6 block">
        <span className="mb-1 block text-[10px] uppercase tracking-widest text-slate-600">
          API key
        </span>
        <input
          type="password"
          value={apiKey}
          onChange={(e) => onApiKeyChange(e.target.value)}
          placeholder="required when OPS_API_KEY is set"
          autoComplete="off"
          className="w-full rounded-md border border-ink-700 bg-ink-800 px-3 py-2 font-mono text-xs text-slate-300 outline-none placeholder:text-slate-600 focus:border-emerald-600/50"
        />
      </label>

      <button
        onClick={onStart}
        disabled={phase === "connecting"}
        className="mt-4 rounded-md border border-emerald-600/40 bg-emerald-600/10 px-3 py-2 text-sm font-medium text-emerald-300 transition hover:bg-emerald-600/20 disabled:opacity-40"
      >
        {threadId ? "+ new thread" : "start session"}
      </button>

      {tasks.length > 0 && (
        <>
          <h2 className="mt-8 text-xs font-semibold uppercase tracking-widest text-slate-500">plan</h2>
          <ul className="mt-2 space-y-2 overflow-y-auto">
            {tasks.map((t) => (
              <li key={t.id} className="rounded-md border border-ink-700 bg-ink-800 p-2.5">
                <div className="flex items-center gap-2">
                  <span className={`inline-block size-1.5 rounded-full ${WORKER_COLORS[t.assignee] ?? "bg-slate-500"}`} />
                  <span className="text-xs font-medium text-slate-300">{t.assignee}</span>
                  <span
                    className={`ml-auto rounded px-1.5 py-0.5 text-[10px] font-medium uppercase ${
                      t.status === "done"
                        ? "bg-emerald-900/60 text-emerald-300"
                        : t.status === "rejected"
                          ? "bg-rose-900/60 text-rose-300"
                          : t.status === "in_progress"
                            ? "animate-pulse bg-sky-900/60 text-sky-300"
                            : "bg-ink-700 text-slate-400"
                    }`}
                  >
                    {t.status}
                  </span>
                </div>
                <p className="mt-1 line-clamp-2 text-xs text-slate-400">{t.description}</p>
              </li>
            ))}
          </ul>
        </>
      )}

      {threadList.length > 0 && (
        <>
          <h2 className="mt-8 text-xs font-semibold uppercase tracking-widest text-slate-500">history</h2>
          <p className="mt-1 text-[10px] text-slate-600">threads for this API key</p>
          <ul className="mt-2 space-y-2 overflow-y-auto">
            {threadList.map((t) => {
              const open = hasThreadToken(t.thread_id);
              return (
                <li key={t.thread_id}>
                  <button
                    onClick={() => onSelectThread(t)}
                    className={`w-full rounded-md border p-2.5 text-left ${
                      t.thread_id === threadId
                        ? "border-emerald-600/50 bg-ink-800"
                        : "border-ink-700 bg-ink-800"
                    }`}
                  >
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-xs text-slate-300">{t.thread_id}</span>
                      <span className="ml-auto text-[10px] uppercase text-slate-500">
                        {t.status === "rejected" ? "rejected" : "done"}
                        {!open ? " · locked" : ""}
                      </span>
                    </div>
                    <p className="mt-1 text-xs text-slate-400">
                      {t.created_at ? new Date(t.created_at).toLocaleString() : "unknown time"}
                      {t.message_count > 0 ? ` · ${t.message_count} messages` : ""}
                    </p>
                  </button>
                </li>
              );
            })}
          </ul>
        </>
      )}

      <div className="mt-auto">
        <p className="mb-1 text-[10px] uppercase tracking-widest text-slate-600">active nodes</p>
        <div className="flex min-h-3 gap-1.5">
          {activeNodes.map((n) => (
            <span key={n} className={`size-2 animate-pulse rounded-full ${WORKER_COLORS[n] ?? "bg-slate-500"}`} title={n} />
          ))}
        </div>
      </div>
    </aside>
  );
}

function MessageBubble({ bubble }: { bubble: Bubble }) {
  if (bubble.role === "system") {
    return (
      <div className="mx-auto w-fit rounded-full border border-rose-800/40 bg-rose-950/40 px-4 py-1 text-xs text-rose-300">
        {bubble.content}
      </div>
    );
  }
  const isUser = bubble.role === "user";
  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-2xl whitespace-pre-wrap rounded-xl px-4 py-3 text-sm leading-relaxed ${
          isUser ? "bg-emerald-700/20 text-emerald-100" : "border border-ink-700 bg-ink-800 text-slate-200"
        }`}
      >
        {bubble.content}
      </div>
    </div>
  );
}

function ApprovalCard({
  threadId,
  payload,
  onDecide,
}: {
  threadId: string;
  payload: ApprovalPayload;
  onDecide: (approved: boolean) => void;
}) {
  // S6: the SQL no longer rides along in the payload. Fetch the exact
  // registered statement on demand so the reviewer still sees precisely
  // what they are approving.
  const [sql, setSql] = useState<string | null>(null);
  const [sqlFailed, setSqlFailed] = useState(false);
  useEffect(() => {
    if (payload.kind === "db_write" && payload.proposal_id != null) {
      setSql(null);
      setSqlFailed(false);
      getProposal(threadId, payload.proposal_id)
        .then((p) => setSql(p.sql))
        .catch(() => setSqlFailed(true));
    }
  }, [threadId, payload.kind, payload.proposal_id]);

  return (
    <div className="mx-auto w-full max-w-2xl overflow-hidden rounded-xl border border-rose-700/50 bg-rose-950/20">
      <div className="flex items-center gap-2 border-b border-rose-800/30 bg-rose-900/20 px-4 py-2">
        <span className="size-2 animate-pulse rounded-full bg-rose-400" />
        <span className="text-xs font-semibold uppercase tracking-wider text-rose-300">
          human approval required
        </span>
      </div>
      <div className="space-y-3 p-4">
        <p className="text-sm text-slate-300">{payload.summary}</p>
        {payload.kind === "db_write" && (
          <pre className="overflow-x-auto rounded-md border border-ink-700 bg-ink-950 p-3 font-mono text-xs text-amber-200">
            {sqlFailed
              ? "could not load the SQL for review; check the API connection"
              : (sql ?? "loading SQL…")}
          </pre>
        )}
        {payload.kind === "github_comment" && payload.body && (
          <blockquote className="rounded-md border border-ink-700 bg-ink-950 p-3 text-xs italic text-slate-300">
            “{payload.body}”
          </blockquote>
        )}
        {payload.rationale && <p className="text-xs text-slate-500">reason given: {payload.rationale}</p>}
        <div className="flex justify-end gap-2 pt-1">
          <button
            onClick={() => onDecide(false)}
            className="rounded-md border border-ink-700 px-4 py-1.5 text-sm text-slate-300 hover:bg-ink-800"
          >
            reject
          </button>
          <button
            onClick={() => onDecide(true)}
            className="rounded-md bg-rose-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-rose-500"
          >
            approve & execute
          </button>
        </div>
      </div>
    </div>
  );
}
