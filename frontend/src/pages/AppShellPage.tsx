import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent, MouseEvent } from "react";
import { useNavigate } from "react-router-dom";
import {
  AlertTriangle,
  Check,
  Database,
  Loader2,
  MessageSquarePlus,
  Pencil,
  Send,
  Terminal,
  Trash2,
  X,
} from "lucide-react";

import ChartAdapter from "../components/ChartAdapter";
import ConnectionsModal from "../components/ConnectionsModal";
import MarkdownMessage from "../components/MarkdownMessage";
import ThemeToggle from "../components/ThemeToggle";
import { ApiError, api } from "../lib/api";
import { initialsFor } from "../lib/avatar";
import { clearTokens } from "../lib/auth";
import { asChartSpec } from "../lib/chart";
import { streamChatMessage } from "../lib/chatStream";
import type { QueryResult } from "../lib/chatStream";
import { engineMeta } from "../lib/engines";
import { useConfigStatus } from "../lib/useConfigStatus";
import type {
  Chat,
  ChartSpec,
  ChatDetail,
  ConnectionOut,
  CurrentUser,
} from "../lib/types";

const ASSISTANT_NAME = "Private Data Assistant";

/** The assistant's circular avatar - the product's own mark, so a reply is
 * attributable at a glance the way the user's initials are. */
function AssistantAvatar() {
  return (
    <div
      title={ASSISTANT_NAME}
      className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-brand-600 text-white"
    >
      <Database size={14} strokeWidth={2.5} />
    </div>
  );
}

/**
 * The query that ran for one turn, collapsed by default.
 *
 * Showing it at all is a deliberate product decision rather than debug
 * output: the whole trust story of a text-to-query assistant is "here is
 * exactly what I asked your database", and a user who knows SQL will want
 * to check it before believing the number.
 */
function QuerySql({ sql, label }: { sql: string; label: string }) {
  return (
    <details className="mt-1.5 rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-1.5 dark:border-slate-700 dark:bg-slate-800/60">
      <summary className="flex cursor-pointer items-center gap-1.5 text-xs font-medium text-slate-500 dark:text-slate-400">
        <Terminal size={12} />
        {label}
      </summary>
      <pre className="mt-1.5 overflow-x-auto whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-slate-600 dark:text-slate-300">
        {sql}
      </pre>
    </details>
  );
}

/**
 * Main chat shell: the chat list, the transcript, the per-chat database
 * selector, and a composer wired to the SSE stream.
 *
 * The live-status pieces (`Querying your database...`, the row count or
 * error that comes back) are pushed by the backend as real events the
 * moment each step happens - see lib/chatStream.ts - not simulated here.
 */
export default function AppShellPage() {
  const navigate = useNavigate();
  const { status: configStatus } = useConfigStatus();

  const [user, setUser] = useState<CurrentUser | null>(null);
  const [chats, setChats] = useState<Chat[]>([]);
  const [selectedChat, setSelectedChat] = useState<ChatDetail | null>(null);
  const [connections, setConnections] = useState<ConnectionOut[]>([]);
  const [loadingChats, setLoadingChats] = useState(true);
  const [loadingConnections, setLoadingConnections] = useState(true);
  const [loadingMessages, setLoadingMessages] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [connectionsOpen, setConnectionsOpen] = useState(false);

  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");

  const [messageInput, setMessageInput] = useState("");
  const [sending, setSending] = useState(false);
  const [streamingReply, setStreamingReply] = useState<string | null>(null);
  // The in-flight turn's query, its result, and any chart - all cleared
  // once the turn finishes and the persisted message replaces them.
  const [liveQuery, setLiveQuery] = useState<string | null>(null);
  const [liveQueryResult, setLiveQueryResult] = useState<QueryResult | null>(null);
  const [liveChart, setLiveChart] = useState<ChartSpec | null>(null);

  const messagesEndRef = useRef<HTMLDivElement>(null);

  const handleAuthFailure = useCallback(() => {
    clearTokens();
    navigate("/login", { replace: true });
  }, [navigate]);

  const loadChats = useCallback(async () => {
    setLoadingChats(true);
    try {
      const [me, chatList] = await Promise.all([
        api.get<CurrentUser>("/api/auth/me", true),
        api.get<Chat[]>("/api/chats", true),
      ]);
      setUser(me);
      setChats(chatList);
      setError(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        handleAuthFailure();
        return;
      }
      setError(err instanceof Error ? err.message : "Failed to load chats.");
    } finally {
      setLoadingChats(false);
    }
  }, [handleAuthFailure]);

  const loadConnections = useCallback(async () => {
    setLoadingConnections(true);
    try {
      const list = await api.get<ConnectionOut[]>("/api/connections", true);
      setConnections(list);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        handleAuthFailure();
        return;
      }
      // Non-fatal: chatting still works, the selector just has nothing in it.
    } finally {
      setLoadingConnections(false);
    }
  }, [handleAuthFailure]);

  useEffect(() => {
    loadChats();
    loadConnections();
  }, [loadChats, loadConnections]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [selectedChat?.messages, streamingReply, liveChart]);

  function connectionFor(connectionId: number | null): ConnectionOut | null {
    if (connectionId === null) return null;
    return connections.find((connection) => connection.id === connectionId) ?? null;
  }

  function clearLiveTurn() {
    setStreamingReply(null);
    setLiveQuery(null);
    setLiveQueryResult(null);
    setLiveChart(null);
  }

  async function selectChat(chatId: number) {
    setLoadingMessages(true);
    setEditingTitle(false);
    clearLiveTurn();
    try {
      const detail = await api.get<ChatDetail>(`/api/chats/${chatId}`, true);
      setSelectedChat(detail);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        handleAuthFailure();
        return;
      }
      setError(err instanceof Error ? err.message : "Failed to load chat.");
    } finally {
      setLoadingMessages(false);
    }
  }

  async function createChat() {
    // The backend enforces "at most one untitled, unbound, empty chat per
    // user" - POST /api/chats returns an existing empty one (with a 200
    // rather than a 201) instead of creating a duplicate. It checks that
    // against the database every time, so always call it rather than
    // guessing client-side from this component's cached list.
    setCreating(true);
    try {
      const chat = await api.post<Chat>("/api/chats", {}, true);
      setChats((prev) => (prev.some((c) => c.id === chat.id) ? prev : [chat, ...prev]));
      await selectChat(chat.id);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        handleAuthFailure();
        return;
      }
      setError(err instanceof Error ? err.message : "Failed to create chat.");
    } finally {
      setCreating(false);
    }
  }

  async function deleteChat(chatId: number, event: MouseEvent) {
    event.stopPropagation();
    try {
      await api.del(`/api/chats/${chatId}`, true);
      setChats((prev) => prev.filter((c) => c.id !== chatId));
      setSelectedChat((prev) => (prev?.id === chatId ? null : prev));
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        handleAuthFailure();
        return;
      }
      setError(err instanceof Error ? err.message : "Failed to delete chat.");
    }
  }

  /**
   * PATCH /api/chats/{id} with ONLY `connection_id`. Presence is what the
   * backend keys off: sending just this one key leaves the title alone,
   * and an explicit null unbinds the chat.
   */
  async function changeChatConnection(chatId: number, rawValue: string) {
    const connectionId = rawValue === "" ? null : Number(rawValue);
    setError(null);
    try {
      const updated = await api.patch<Chat>(
        `/api/chats/${chatId}`,
        { connection_id: connectionId },
        true,
      );
      setSelectedChat((prev) =>
        prev && prev.id === chatId ? { ...prev, connection_id: updated.connection_id } : prev,
      );
      setChats((prev) =>
        prev.map((c) => (c.id === chatId ? { ...c, connection_id: updated.connection_id } : c)),
      );
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        handleAuthFailure();
        return;
      }
      setError(
        err instanceof Error ? err.message : "Could not change this chat's database.",
      );
    }
  }

  /** PATCH with ONLY `title` - same presence rule, so the chat's database
   * binding is untouched by a rename. */
  async function renameChat(event: FormEvent) {
    event.preventDefault();
    if (!selectedChat) return;
    const title = titleDraft.trim();
    if (!title || title === selectedChat.title) {
      setEditingTitle(false);
      return;
    }
    const chatId = selectedChat.id;
    try {
      const updated = await api.patch<Chat>(`/api/chats/${chatId}`, { title }, true);
      setSelectedChat((prev) =>
        prev && prev.id === chatId ? { ...prev, title: updated.title } : prev,
      );
      setChats((prev) => prev.map((c) => (c.id === chatId ? { ...c, title: updated.title } : c)));
      setEditingTitle(false);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        handleAuthFailure();
        return;
      }
      setError(err instanceof Error ? err.message : "Could not rename this chat.");
    }
  }

  /** Deleting a connection nulls `chat.connection_id` server-side (the
   * FK's ON DELETE SET NULL), so the chat list and the open chat both have
   * to be re-read, not just the connection list. */
  async function handleConnectionsChanged() {
    const openChatId = selectedChat?.id ?? null;
    await Promise.all([loadConnections(), loadChats()]);
    if (openChatId !== null) {
      try {
        const detail = await api.get<ChatDetail>(`/api/chats/${openChatId}`, true);
        setSelectedChat(detail);
      } catch {
        // The chat itself is unaffected by a connection change failing to
        // re-read - leave what's on screen.
      }
    }
  }

  function logout() {
    clearTokens();
    navigate("/login", { replace: true });
  }

  async function handleSendMessage(event: FormEvent) {
    event.preventDefault();
    const content = messageInput.trim();
    if (!content || !selectedChat || sending) return;

    const chatId = selectedChat.id;
    setSending(true);
    clearLiveTurn();
    setStreamingReply("");
    setMessageInput("");
    setError(null);

    // Optimistic: show the user's own message immediately rather than
    // waiting for the stream to finish and a refetch to bring it back.
    setSelectedChat((prev) =>
      prev && prev.id === chatId
        ? {
            ...prev,
            messages: [
              ...prev.messages,
              {
                id: -Date.now(),
                role: "user",
                content,
                query_sql: null,
                chart_spec: null,
                created_at: new Date().toISOString(),
              },
            ],
          }
        : prev,
    );

    // `finish` reloads the chat from the server (so the optimistic user
    // message and the streamed reply are replaced by the real, persisted
    // rows - including the query_sql and chart_spec the backend saved) and
    // clears the in-progress UI state. Called from onDone when the stream
    // completes normally, and again unconditionally after
    // streamChatMessage() resolves as a fallback for the case where the
    // initial request failed and no "done" event was ever sent - the
    // `finished` guard makes calling it twice harmless.
    let finished = false;
    const finish = async () => {
      if (finished) return;
      finished = true;
      try {
        const detail = await api.get<ChatDetail>(`/api/chats/${chatId}`, true);
        setSelectedChat(detail);
        // The backend auto-titles a chat from its first message - keep the
        // sidebar list (separate state from the selected chat's detail) in
        // sync so the new title shows there too, not just in the header.
        setChats((prev) => prev.map((c) => (c.id === chatId ? { ...c, title: detail.title } : c)));
      } catch {
        // Keep the optimistic/streamed content on screen if the refetch
        // itself fails - not worth surfacing a second error.
      }
      clearLiveTurn();
      setSending(false);
    };

    await streamChatMessage(chatId, content, {
      onToken: (text) => setStreamingReply((prev) => (prev ?? "") + text),
      onQuery: (query) => {
        setLiveQuery(query);
        setLiveQueryResult(null);
      },
      onQueryResult: (result) => setLiveQueryResult(result),
      onChart: (spec) => setLiveChart(spec),
      onError: (message) => setError(message),
      onDone: () => {
        void finish();
      },
    });

    await finish();
  }

  const aiConfigured = configStatus?.connections_llm ?? true; // avoid a flash of "disabled" while loading
  const encryptionConfigured = configStatus?.encryption ?? true;
  const composerDisabled = !selectedChat || sending || !aiConfigured;
  const selectedConnection = selectedChat ? connectionFor(selectedChat.connection_id) : null;

  return (
    <div className="flex h-screen bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <aside className="flex w-72 flex-col border-r border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
        <div className="flex items-center justify-between border-b border-slate-200 px-4 py-4 dark:border-slate-800">
          <div className="flex items-center gap-2">
            <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-brand-600 text-white">
              <Database size={14} strokeWidth={2.5} />
            </div>
            <span className="text-sm font-bold leading-tight tracking-tight dark:text-white">
              Private Data Assistant
            </span>
          </div>
          <ThemeToggle compact />
        </div>

        <div className="flex flex-col gap-2 p-3">
          <button
            type="button"
            onClick={createChat}
            disabled={creating}
            className="flex w-full items-center justify-center gap-2 rounded-lg bg-brand-600 px-3 py-2 text-sm font-medium text-white shadow-card transition hover:bg-brand-700 disabled:opacity-50"
          >
            <MessageSquarePlus size={16} />
            {creating ? "Creating..." : "New chat"}
          </button>
          <button
            type="button"
            onClick={() => setConnectionsOpen(true)}
            title="Register a database, test it, re-index its schema, or remove it"
            className="flex w-full items-center justify-center gap-2 rounded-lg border border-slate-300 px-3 py-2 text-sm font-medium text-slate-700 shadow-sm transition hover:bg-slate-50 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
          >
            <Database size={16} />
            My databases
            {connections.length > 0 && (
              <span className="rounded-full bg-slate-100 px-1.5 text-xs font-semibold text-slate-500 dark:bg-slate-800 dark:text-slate-400">
                {connections.length}
              </span>
            )}
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-2">
          {loadingChats && (
            <p className="px-2 py-2 text-sm text-slate-400 dark:text-slate-500">
              Loading chats...
            </p>
          )}
          {!loadingChats && chats.length === 0 && (
            <p className="px-2 py-2 text-sm text-slate-400 dark:text-slate-500">
              No chats yet - create one above.
            </p>
          )}
          {chats.map((chat) => {
            const bound = connectionFor(chat.connection_id);
            return (
              <button
                key={chat.id}
                type="button"
                onClick={() => selectChat(chat.id)}
                className={`group mb-1 flex w-full items-center justify-between gap-2 rounded-lg px-3 py-2 text-left text-sm transition ${
                  selectedChat?.id === chat.id
                    ? "bg-brand-50 font-medium text-brand-800 dark:bg-brand-950 dark:text-brand-300"
                    : "text-slate-700 hover:bg-slate-50 dark:text-slate-300 dark:hover:bg-slate-800"
                }`}
              >
                <span className="min-w-0 flex-1">
                  <span className="block truncate">{chat.title}</span>
                  <span className="mt-0.5 flex items-center gap-1 truncate text-xs font-normal text-slate-400 dark:text-slate-500">
                    <Database size={10} className="shrink-0" />
                    {bound ? bound.name : "No database"}
                  </span>
                </span>
                <span
                  role="button"
                  tabIndex={0}
                  title="Delete this chat"
                  onClick={(e) => deleteChat(chat.id, e)}
                  className="hidden shrink-0 text-slate-400 hover:text-red-600 group-hover:inline dark:text-slate-500 dark:hover:text-red-400"
                >
                  <Trash2 size={14} />
                </span>
              </button>
            );
          })}
        </div>

        <div className="border-t border-slate-200 p-3 dark:border-slate-800">
          <p className="truncate px-1 text-xs text-slate-500 dark:text-slate-400">
            {user?.email ?? "..."}
          </p>
          <button
            type="button"
            onClick={logout}
            className="mt-2 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm font-medium text-slate-700 shadow-sm transition hover:bg-slate-50 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
          >
            Log out
          </button>
        </div>
      </aside>

      <main className="flex min-w-0 flex-1 flex-col">
        {configStatus && !configStatus.connections_llm && (
          <div className="border-b border-amber-200 bg-amber-50 px-4 py-2 text-sm text-amber-800 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-300">
            Configuration missing - set Azure OpenAI embeddings credentials (AZURE_EM_*)
            and{" "}
            {configStatus.llm_provider === "azure" ? "Azure OpenAI chat" : "Groq chat"}{" "}
            credentials ({configStatus.llm_provider === "azure" ? "LLM_ENDPOINT*" : "GROQ_API_KEY"})
            in .env to enable registering a database and asking questions.
          </div>
        )}

        {error && (
          <div className="border-b border-amber-200 bg-amber-50 px-4 py-2 text-sm text-amber-800 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-300">
            {error}
          </div>
        )}

        {!selectedChat && (
          <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 text-center text-sm text-slate-400 dark:text-slate-500">
            <div className="flex h-12 w-12 items-center justify-center rounded-full bg-brand-50 text-brand-500 dark:bg-brand-950 dark:text-brand-400">
              <Database size={20} />
            </div>
            Select a chat on the left, or create a new one.
            {connections.length === 0 && !loadingConnections && (
              <span>
                You haven't registered a database yet -{" "}
                <button
                  type="button"
                  onClick={() => setConnectionsOpen(true)}
                  className="font-medium text-brand-600 underline hover:text-brand-700 dark:text-brand-400"
                >
                  add one
                </button>{" "}
                to start asking questions about your data.
              </span>
            )}
          </div>
        )}

        {selectedChat && (
          <>
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 px-6 py-4 dark:border-slate-800">
              {editingTitle ? (
                <form onSubmit={renameChat} className="flex min-w-0 flex-1 items-center gap-1.5">
                  <input
                    autoFocus
                    type="text"
                    value={titleDraft}
                    onChange={(e) => setTitleDraft(e.target.value)}
                    className="min-w-0 flex-1 rounded-lg border border-slate-300 bg-white px-2 py-1 text-sm font-semibold text-slate-900 outline-none focus:border-brand-500 focus:ring-1 focus:ring-brand-500 dark:border-slate-700 dark:bg-slate-800 dark:text-white"
                  />
                  <button
                    type="submit"
                    title="Save"
                    className="rounded-full p-1.5 text-slate-400 hover:bg-slate-100 hover:text-brand-600 dark:hover:bg-slate-800 dark:hover:text-brand-400"
                  >
                    <Check size={15} />
                  </button>
                  <button
                    type="button"
                    title="Cancel"
                    onClick={() => setEditingTitle(false)}
                    className="rounded-full p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-600 dark:hover:bg-slate-800 dark:hover:text-slate-300"
                  >
                    <X size={15} />
                  </button>
                </form>
              ) : (
                <div className="flex min-w-0 items-center gap-1.5">
                  <h2 className="truncate font-semibold dark:text-white">{selectedChat.title}</h2>
                  <button
                    type="button"
                    title="Rename this chat"
                    onClick={() => {
                      setTitleDraft(selectedChat.title);
                      setEditingTitle(true);
                    }}
                    className="shrink-0 rounded-full p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-600 dark:hover:bg-slate-800 dark:hover:text-slate-300"
                  >
                    <Pencil size={13} />
                  </button>
                </div>
              )}

              <div className="flex shrink-0 items-center gap-2">
                <label
                  htmlFor="chat-connection"
                  className="text-xs font-medium text-slate-500 dark:text-slate-400"
                >
                  Database
                </label>
                <select
                  id="chat-connection"
                  value={selectedChat.connection_id === null ? "" : String(selectedChat.connection_id)}
                  onChange={(e) => changeChatConnection(selectedChat.id, e.target.value)}
                  className="max-w-[16rem] rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-sm text-slate-800 shadow-sm transition focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"
                >
                  <option value="">No database</option>
                  {connections.map((connection) => (
                    <option
                      key={connection.id}
                      value={connection.id}
                      disabled={connection.status !== "ready"}
                    >
                      {connection.name} ({engineMeta(connection.engine).label})
                      {connection.status !== "ready" ? ` - ${connection.status}` : ""}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  onClick={() => setConnectionsOpen(true)}
                  className="rounded-lg border border-slate-300 px-2.5 py-1.5 text-xs font-medium text-slate-600 shadow-sm transition hover:bg-slate-50 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
                >
                  Manage
                </button>
              </div>
            </div>

            <div className="flex-1 overflow-y-auto px-6 py-4">
              {loadingMessages && (
                <p className="text-sm text-slate-400 dark:text-slate-500">Loading messages...</p>
              )}
              {!loadingMessages &&
                selectedChat.messages.length === 0 &&
                streamingReply === null && (
                  <p className="text-sm text-slate-400 dark:text-slate-500">
                    No messages yet.{" "}
                    {selectedConnection
                      ? `Ask something about ${selectedConnection.name} - "how many rows are in each table?" is a good first question.`
                      : "Pick a database above, then ask a question about it."}
                  </p>
                )}

              <div className="flex flex-col">
                {selectedChat.messages.map((message, index) => {
                  const isUser = message.role === "user";
                  // A user message always starts a new Q&A turn (more space
                  // above, separating it from the previous turn's reply);
                  // the assistant reply right after stays visually attached
                  // to its own question.
                  const topSpacing = index === 0 ? "" : isUser ? "mt-5" : "mt-1.5";
                  const historyChart = asChartSpec(message.chart_spec);
                  return (
                    <div
                      key={message.id}
                      className={`flex max-w-2xl items-end gap-2 ${topSpacing} ${
                        isUser ? "ml-auto flex-row-reverse" : ""
                      }`}
                    >
                      {isUser ? (
                        <div
                          title={user?.full_name ?? user?.email ?? "You"}
                          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-slate-700 text-xs font-semibold text-white dark:bg-slate-600"
                        >
                          {initialsFor(user?.full_name, user?.email ?? "?")}
                        </div>
                      ) : (
                        <AssistantAvatar />
                      )}
                      <div className="flex min-w-0 flex-col gap-1">
                        {!isUser && (
                          <span className="px-1 text-xs font-medium text-slate-400 dark:text-slate-500">
                            {ASSISTANT_NAME}
                          </span>
                        )}
                        <div
                          className={`rounded-lg px-4 py-2 text-sm ${
                            isUser
                              ? "whitespace-pre-wrap bg-brand-600 text-white"
                              : "bg-white text-slate-900 shadow-card dark:bg-slate-900 dark:text-slate-100"
                          }`}
                        >
                          {isUser ? (
                            message.content
                          ) : (
                            <>
                              <MarkdownMessage content={message.content} />
                              {/* Re-validated rather than trusted: the
                                  backend stores chart_spec as free-form
                                  JSON, so a row written by an older build
                                  can't be assumed to still match. */}
                              {historyChart && <ChartAdapter spec={historyChart} />}
                              {message.query_sql && (
                                <QuerySql sql={message.query_sql} label="Query used" />
                              )}
                            </>
                          )}
                        </div>
                      </div>
                    </div>
                  );
                })}

                {streamingReply !== null && (
                  <div
                    className={`flex max-w-2xl items-end gap-2 ${
                      selectedChat.messages.length === 0 ? "" : "mt-1.5"
                    }`}
                  >
                    <AssistantAvatar />
                    <div className="flex min-w-0 flex-col gap-1">
                      <span className="px-1 text-xs font-medium text-slate-400 dark:text-slate-500">
                        {ASSISTANT_NAME}
                      </span>

                      {/* Live query status: the `query` event fires BEFORE
                          the query runs, `query_result` right after it. */}
                      {liveQuery !== null && (
                        <div className="rounded-lg bg-white px-4 py-2 text-sm shadow-card dark:bg-slate-900">
                          <div className="flex items-center gap-2 text-slate-500 dark:text-slate-400">
                            {liveQueryResult === null ? (
                              <>
                                <Loader2 size={13} className="animate-spin text-brand-500" />
                                <span>Querying your database...</span>
                              </>
                            ) : liveQueryResult.ok ? (
                              <>
                                <Check size={13} className="text-emerald-500" />
                                <span>
                                  Query returned {liveQueryResult.row_count}{" "}
                                  {liveQueryResult.row_count === 1 ? "row" : "rows"}.
                                </span>
                              </>
                            ) : (
                              <>
                                <AlertTriangle size={13} className="shrink-0 text-red-500" />
                                <span className="text-red-600 dark:text-red-400">
                                  {liveQueryResult.error ?? "The query failed."}
                                </span>
                              </>
                            )}
                          </div>
                          {liveQuery && <QuerySql sql={liveQuery} label="Show the query" />}
                        </div>
                      )}

                      {(streamingReply !== "" || liveQuery === null) && (
                        <div className="rounded-lg bg-white px-4 py-2 text-sm text-slate-900 shadow-card dark:bg-slate-900 dark:text-slate-100">
                          {streamingReply === "" ? (
                            <div className="flex items-center gap-2 text-slate-500 dark:text-slate-400">
                              <Loader2 size={13} className="animate-spin text-brand-500" />
                              <span>Thinking...</span>
                            </div>
                          ) : (
                            <>
                              <MarkdownMessage content={`${streamingReply}▍`} />
                              {liveChart && <ChartAdapter spec={liveChart} />}
                            </>
                          )}
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>
              <div ref={messagesEndRef} />
            </div>

            <form
              onSubmit={handleSendMessage}
              className="border-t border-slate-200 px-4 pb-8 pt-3 dark:border-slate-800"
            >
              {/* A hint, not a blocker: the backend answers greetings and
                  product questions perfectly well with no database bound,
                  and only a genuine data question needs one. */}
              {!selectedConnection && aiConfigured && (
                <p className="mb-2 text-xs text-slate-400 dark:text-slate-500">
                  Select a database above to ask questions about your data. Without one,
                  the assistant can still chat - it just has nothing to query.
                </p>
              )}
              {selectedConnection && selectedConnection.status !== "ready" && (
                <p className="mb-2 text-xs text-amber-600 dark:text-amber-400">
                  {selectedConnection.name} is <strong>{selectedConnection.status}</strong> -
                  its schema isn't indexed, so data questions may not work yet.
                </p>
              )}

              <div className="flex items-center gap-1 rounded-2xl border border-slate-300 bg-white px-2 py-1.5 shadow-sm transition focus-within:border-brand-500 focus-within:ring-1 focus-within:ring-brand-500 dark:border-slate-700 dark:bg-slate-900">
                <input
                  type="text"
                  value={messageInput}
                  onChange={(e) => setMessageInput(e.target.value)}
                  disabled={composerDisabled}
                  placeholder={
                    aiConfigured
                      ? selectedConnection
                        ? `Ask ${selectedConnection.name} a question...`
                        : "Ask a question..."
                      : "Configuration missing - set AI credentials in .env"
                  }
                  className="min-w-0 flex-1 border-0 bg-transparent px-2 py-1.5 text-sm text-slate-900 outline-none disabled:cursor-not-allowed disabled:text-slate-400 dark:text-white dark:disabled:text-slate-500"
                />
                <button
                  type="submit"
                  disabled={composerDisabled || !messageInput.trim()}
                  title="Send"
                  className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-brand-600 text-white transition hover:bg-brand-700 disabled:cursor-not-allowed disabled:bg-slate-200 disabled:text-slate-400 dark:disabled:bg-slate-800 dark:disabled:text-slate-600"
                >
                  {sending ? <Loader2 size={15} className="animate-spin" /> : <Send size={15} />}
                </button>
              </div>
            </form>
          </>
        )}
      </main>

      <ConnectionsModal
        open={connectionsOpen}
        onClose={() => setConnectionsOpen(false)}
        connections={connections}
        loading={loadingConnections}
        onChanged={handleConnectionsChanged}
        onAuthFailure={handleAuthFailure}
        encryptionConfigured={encryptionConfigured}
        aiConfigured={aiConfigured}
      />
    </div>
  );
}
