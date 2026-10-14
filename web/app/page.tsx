"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  AgentEvent,
  ApprovalPayload,
  TaskView,
  API_URL,
  createThread,
  getProposal,
  getThreadState,
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
  const [phase, setPhase] = useState<Phase>("idle");
  const [input, setInput] = useState("");
  // Kept in React state only: never logged, never persisted.
  const [apiKey, setApiKeyState] = useState("");
  const socketRef = useRef<WebSocket | null>(null);
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

  async function refreshState() {
    if (!threadId) return;
    try {
      const data = await getThreadState(threadId);
      setTasks(data.tasks ?? []);
      if (!data.awaiting) {
        setActiveNodes([]);
        setPhase("idle");
      }
    } catch {
      setPhase("idle"); // state refresh is best-effort
    }
  }

  const startThread = useCallback(async () => {
    setPhase("connecting");
    try {
      const { threadId: id } = await createThread();
      const ws = new WebSocket(
        `${API_URL.replace(/^http/, "ws")}/ws/${id}`,
        wsSubprotocols(id),
      );
      socketRef.current = ws;
      const failTimer = window.setTimeout(() => {
        setBubbles((prev) => [
          ...prev,
          { role: "system", content: "error: could not reach the API websocket, is the backend up?" },
        ]);
        setPhase("idle");
      }, 8000);
      ws.onopen = () => {
        window.clearTimeout(failTimer);
        setThreadId(id);
        setBubbles([]);
        setTasks([]);
        setPending(null);
        setActiveNodes([]);
        setPhase("idle");
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
    } catch (err) {
      setBubbles((prev) => [
        ...prev,
        { role: "system", content: `error: could not create a thread (${String(err).slice(0, 120)})` },
      ]);
      setPhase("idle");
    }
  }, [handleEvent]);

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
    setPending(null);
    setPhase("running");
    const ok = await submitApproval(threadId, approved, approved ? "" : "declined from console");
    if (!ok) setPhase("awaiting_approval");
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
      />

      <section className="flex flex-1 flex-col">
        {!threadId ? (
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
}: {
  threadId: string | null;
  phase: Phase;
  tasks: TaskView[];
  activeNodes: string[];
  apiKey: string;
  onApiKeyChange: (value: string) => void;
  onStart: () => void;
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
