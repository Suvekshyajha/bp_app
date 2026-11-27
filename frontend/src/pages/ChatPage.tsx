import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Identity } from "../types";
import { getHealth } from "../api";
import { RateLimitError, useChatSocket } from "../hooks/useChatSocket";
import { useConversations } from "../hooks/useConversations";
import { useChatMessages } from "../hooks/useChatMessages";
import { useDocuments } from "../hooks/useDocuments";
import { Sidebar } from "../components/Sidebar";
import { ConnectionStatus } from "../components/ConnectionStatus";
import { ErrorBanner } from "../components/ErrorBanner";
import { MessageList } from "../components/MessageList";
import { ChatInput } from "../components/ChatInput";
import { DocumentPanel } from "../components/DocumentPanel";
import { Toasts } from "../components/toasts";
import {
  CloseIcon,
  FileIcon,
  MenuIcon,
  PaperclipIcon,
} from "../components/Icons";
import { STATUS_LABELS, isBusy } from "../components/documentStatus";
import { ACCEPTED_EXTENSIONS } from "../services/documentLimits";

export function ChatPage({
  identity,
  onLogout,
}: {
  identity: Identity;
  onLogout: () => void;
}) {
  const {
    conversations,
    selectedId,
    status: listStatus,
    hasMore,
    loadingMore,
    loadMore,
    retryLoad: retryConversations,
    createConversation,
    selectConversation,
    touchConversation,
    markSynced,
    deleteConversation,
  } = useConversations(onLogout);

  // A brand-new chat the server has not seen has no history to load.
  const selectedIsLocal =
    conversations.find((conversation) => conversation.id === selectedId)
      ?.isLocal ?? false;

  const {
    messages,
    historyStatus,
    hasEarlier,
    loadingEarlier,
    loadEarlier,
    retryLoad: retryMessages,
    addMessage,
    updateMessage,
    removeMessage,
    removeAfter,
    removeChat,
  } = useChatMessages(selectedId, selectedIsLocal, onLogout);

  const { status, send, cancel } = useChatSocket(onLogout);

  const {
    documents,
    status: documentStatus,
    uploading,
    error: documentError,
    upload: uploadDocument,
    remove: removeDocument,
    refresh: reloadDocuments,
    dismissError: dismissDocumentError,
    notices: documentNotices,
    dismissNotice: dismissDocumentNotice,
  } = useDocuments(onLogout);

  // The documents the user ticked. Only ready documents count, so a document
  // that was deleted or is not ready is dropped here without extra bookkeeping.
  const [pickedDocIds, setPickedDocIds] = useState<readonly string[]>([]);

  const selectedDocIds = useMemo(() => {
    const ready = new Set(
      documents.filter((d) => d.status === "ready").map((d) => d.id),
    );
    return pickedDocIds.filter((id) => ready.has(id));
  }, [documents, pickedDocIds]);

  const toggleDocument = useCallback((id: string) => {
    setPickedDocIds((current) =>
      current.includes(id)
        ? current.filter((x) => x !== id)
        : [...current, id],
    );
  }, []);

  const [draft, setDraft] = useState("");

  // ---- Documents in the chat box -------------------------------------
  const [docsOpen, setDocsOpen] = useState(false);
  const [dragging, setDragging] = useState(false);
  const composerRef = useRef<HTMLDivElement>(null);
  const attachInputRef = useRef<HTMLInputElement>(null);

  // Esc or a click outside the chat box closes the documents popover
  useEffect(() => {
    if (!docsOpen) return;

    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setDocsOpen(false);
    };

    const onPointer = (event: MouseEvent) => {
      if (
        composerRef.current &&
        !composerRef.current.contains(event.target as Node)
      ) {
        setDocsOpen(false);
      }
    };

    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onPointer);

    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onPointer);
    };
  }, [docsOpen]);

  const attachFiles = (files: FileList | null | undefined) => {
    const file = files?.[0];
    if (file) void uploadDocument(file);
  };

  // Which chats are waiting for an answer.
  // Several chats can wait at the same time.
  const [sendingIds, setSendingIds] = useState<ReadonlySet<string>>(new Set());
  const [apiError, setApiError] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);

  const sendingRef = useRef<Set<string>>(new Set());

  // After a rate-limit response, sending is paused until this time.
  const [cooldownUntil, setCooldownUntil] = useState(0);
  const [now, setNow] = useState(() => Date.now());

  const cooldownSeconds = Math.max(
    0,
    Math.ceil((cooldownUntil - now) / 1000),
  );

  useEffect(() => {
    if (cooldownUntil <= Date.now()) return;

    const timer = window.setInterval(() => {
      const current = Date.now();
      setNow(current);

      if (current >= cooldownUntil) {
        window.clearInterval(timer);
      }
    }, 500);

    return () => window.clearInterval(timer);
  }, [cooldownUntil]);

  const isSending = sendingIds.has(selectedId);

  const markSending = useCallback((id: string, on: boolean) => {
    if (on) {
      sendingRef.current.add(id);
    } else {
      sendingRef.current.delete(id);
    }

    setSendingIds(new Set(sendingRef.current));
  }, []);

  useEffect(() => {
    let active = true;

    getHealth()
      .then(() => {
        if (active) setApiError(false);
      })
      .catch(() => {
        if (active) setApiError(true);
      });

    return () => {
      active = false;
    };
  }, []);

  // Sends a message and streams the assistant response.
  // Can also be used to retry an existing user message.
  //
  // messageId is the idempotency key. It is sent with every request, so if
  // the same question reaches the server twice (a retry after the connection
  // dropped) it gets the saved answer back instead of a second one.
  // regenerate = true means "I want a NEW answer to this same question".
  const sendExistingMessage = useCallback(
    async (
      query: string,
      messageId: string,
      conversationId: string,
      regenerate = false,
    ) => {
      if (sendingRef.current.has(conversationId)) return;

      markSending(conversationId, true);

      updateMessage(conversationId, messageId, (message) => ({
        ...message,
        status: "pending",
        error: undefined,
      }));

      // The assistant bubble is created when the first token arrives.
      const assistantId = crypto.randomUUID();
      let started = false;

      const handleToken = (token: string) => {
        if (!started) {
          started = true;

          addMessage(conversationId, {
            id: assistantId,
            role: "assistant",
            content: token,
            createdAt: new Date().toISOString(),
          });
        } else {
          updateMessage(conversationId, assistantId, (message) => ({
            ...message,
            content: message.content + token,
          }));
        }
      };

      try {
        const response = await send(
          {
            query,
            conversation_id: conversationId,
            user_message_id: messageId,
            regenerate,
            // Left out when nothing is ticked: no document search at all
            ...(selectedDocIds.length > 0
              ? { document_ids: [...selectedDocIds] }
              : {}),
          },
          handleToken,
        );

        updateMessage(conversationId, messageId, (message) => ({
          ...message,
          status: "sent",
          error: undefined,
        }));

        markSynced(conversationId);

        if (started) {
          // Finish the assistant bubble we were streaming into.
          updateMessage(conversationId, assistantId, (message) => ({
            ...message,
            content: response.answer,
            context: response.results,
          }));
        } else if (response.answer) {
          addMessage(conversationId, {
            id: assistantId,
            role: "assistant",
            content: response.answer,
            createdAt: new Date().toISOString(),
            context: response.results,
          });
        }
      } catch (error) {
        // Remove a partially written answer if the request fails.
        if (started) {
          removeMessage(conversationId, assistantId);
        }

        if (error instanceof RateLimitError) {
          const until = Date.now() + error.retryAfter * 1000;
          setNow(Date.now());
          setCooldownUntil(until);
        }

        const message =
          error instanceof Error
            ? error.message
            : "Something went wrong while sending your message.";

        updateMessage(conversationId, messageId, (current) => ({
          ...current,
          status: "failed",
          error: message,
        }));
      } finally {
        markSending(conversationId, false);
      }
    },
    [
      addMessage,
      markSending,
      markSynced,
      removeMessage,
      selectedDocIds,
      send,
      updateMessage,
    ],
  );

  // Send a brand-new message.
  const handleSend = useCallback(
    (rawText?: string) => {
      const query = (rawText ?? draft).trim().slice(0, 4000);

      if (!query || sendingRef.current.has(selectedId) || !selectedId) {
        return;
      }

      // Wait until history is loaded and respect the rate limit.
      if (historyStatus !== "ready" || cooldownSeconds > 0) {
        return;
      }

      const messageId = crypto.randomUUID();
      const conversationId = selectedId;

      addMessage(conversationId, {
        id: messageId,
        role: "user",
        content: query,
        createdAt: new Date().toISOString(),
        status: "pending",
      });

      touchConversation(conversationId, query);
      setDraft("");
      // The documents popover has done its job once the question is sent
      setDocsOpen(false);

      void sendExistingMessage(query, messageId, conversationId);
    },
    [
      addMessage,
      cooldownSeconds,
      draft,
      historyStatus,
      selectedId,
      sendExistingMessage,
      touchConversation,
    ],
  );

  // Retry a failed user message.
  const handleRetry = useCallback(
    (messageId: string) => {
      const failedMessage = messages.find(
        (message) => message.id === messageId,
      );

      if (
        cooldownSeconds > 0 ||
        sendingRef.current.has(selectedId)
      ) {
        return;
      }

      if (
        !failedMessage ||
        failedMessage.role !== "user" ||
        failedMessage.status !== "failed"
      ) {
        return;
      }

      touchConversation(selectedId);

      void sendExistingMessage(
        failedMessage.content,
        failedMessage.id,
        selectedId,
      );
    },
    [
      cooldownSeconds,
      messages,
      selectedId,
      sendExistingMessage,
      touchConversation,
    ],
  );

  // Edit an existing user message and generate a new response.
  const handleEdit = useCallback(
    (messageId: string, text: string): boolean => {
      const editedMessage = messages.find(
        (message) => message.id === messageId,
      );

      const query = text.trim().slice(0, 4000);

      if (
        !query ||
        !selectedId ||
        cooldownSeconds > 0 ||
        sendingRef.current.has(selectedId)
      ) {
        return false;
      }

      if (!editedMessage || editedMessage.role !== "user") {
        return false;
      }

      // Remove the old assistant response and all messages after
      // the message being edited.
      removeAfter(selectedId, messageId);

      // Update the existing user message with the edited text.
      updateMessage(selectedId, messageId, (message) => ({
        ...message,
        content: query,
        status: "pending",
        error: undefined,
      }));

      touchConversation(selectedId, query);

      // Generate a new response using the edited text.
      void sendExistingMessage(query, messageId, selectedId);

      return true;
    },
    [
      cooldownSeconds,
      messages,
      removeAfter,
      selectedId,
      sendExistingMessage,
      touchConversation,
      updateMessage,
    ],
  );

  // Regenerate the assistant response for the preceding user message.
  const handleRegenerate = useCallback(
    (assistantMessageId: string) => {
      if (
        !selectedId ||
        cooldownSeconds > 0 ||
        sendingRef.current.has(selectedId)
      ) {
        return;
      }

      const assistantIndex = messages.findIndex(
        (message) => message.id === assistantMessageId,
      );

      if (assistantIndex === -1 || assistantIndex === 0) {
        return;
      }

      const assistantMessage = messages[assistantIndex];
      const userMessage = messages[assistantIndex - 1];

      // Regenerate is only valid for an assistant message
      // immediately following a user message.
      if (
        assistantMessage.role !== "assistant" ||
        userMessage.role !== "user"
      ) {
        return;
      }

      // Keep the user message but remove the old assistant response
      // and anything that came after it.
      removeAfter(selectedId, userMessage.id);

      touchConversation(selectedId);

      // Generate a fresh assistant response (true = do not replay the old one).
      void sendExistingMessage(
        userMessage.content,
        userMessage.id,
        selectedId,
        true,
      );
    },
    [
      cooldownSeconds,
      messages,
      removeAfter,
      selectedId,
      sendExistingMessage,
      touchConversation,
    ],
  );

  // ---- Pieces of the chat box (attachments and buttons) ----------------
  const attachedDocs = selectedDocIds.flatMap((id) => {
    const found = documents.find((item) => item.id === id);
    return found ? [found] : [];
  });

  const busyDocs = documents.filter((item) => isBusy(item.status));
  const showDocError = Boolean(documentError) && !docsOpen;

  const attachmentBar =
    attachedDocs.length > 0 ||
    busyDocs.length > 0 ||
    uploading ||
    showDocError ? (
      <div className="doc-chips" aria-label="Attached documents">
        {uploading && (
          <span className="doc-chip is-busy">
            <span className="doc-spinner" aria-hidden="true" />
            <span className="doc-chip-name">Uploading…</span>
          </span>
        )}

        {busyDocs.map((item) => (
          <span className="doc-chip is-busy" key={item.id} title={item.filename}>
            <span className="doc-spinner" aria-hidden="true" />
            <span className="doc-chip-name">{item.filename}</span>
            <span className="doc-chip-state">{STATUS_LABELS[item.status]}</span>
          </span>
        ))}

        {attachedDocs.map((item) => (
          <span className="doc-chip" key={item.id} title={item.filename}>
            <FileIcon />
            <span className="doc-chip-name">{item.filename}</span>
            <button
              type="button"
              aria-label={"Stop asking about " + item.filename}
              onClick={() => toggleDocument(item.id)}
            >
              <CloseIcon />
            </button>
          </span>
        ))}

        {showDocError && (
          <span className="doc-chip is-error" role="alert">
            <span className="doc-chip-name">{documentError}</span>
            <button
              type="button"
              aria-label="Dismiss"
              onClick={dismissDocumentError}
            >
              <CloseIcon />
            </button>
          </span>
        )}
      </div>
    ) : null;

  const composerTools = (
    <>
      <input
        ref={attachInputRef}
        id="composer-file-input"
        className="sr-only"
        type="file"
        accept={ACCEPTED_EXTENSIONS.join(",")}
        tabIndex={-1}
        onChange={(event) => {
          const files = event.target.files;
          attachFiles(files);
          event.target.value = "";
        }}
      />

      <button
        type="button"
        className="composer-tool"
        disabled={uploading}
        aria-label="Attach a document"
        title="Attach a document (PDF, DOCX, TXT, MD)"
        onClick={() => attachInputRef.current?.click()}
      >
        <PaperclipIcon />
      </button>

      <button
        type="button"
        className={"composer-tool composer-tool--text" + (docsOpen ? " is-active" : "")}
        aria-expanded={docsOpen}
        onClick={() => setDocsOpen((open) => !open)}
      >
        Documents
        {selectedDocIds.length > 0 && (
          <span className="composer-badge">{selectedDocIds.length}</span>
        )}
      </button>
    </>
  );

  const handleLogout = () => {
    const confirmed = window.confirm("Are you sure you want to log out?");

    if (confirmed) {
      onLogout();
    }
  };

  const handleDelete = (id: string) => {
    const confirmed = window.confirm(
      "Remove this chat from your list in this browser? Your saved memory on the server is kept.",
    );

    if (confirmed) {
      cancel(id);
      removeChat(id);
      deleteConversation(id);
    }
  };

  const handleSelect = (id: string) => {
    selectConversation(id);
    setSidebarOpen(false);
  };

  return (
    <main className="chat-app">
      <button
        type="button"
        className={"mobile-scrim " + (sidebarOpen ? "is-visible" : "")}
        aria-label="Close conversations menu"
        onClick={() => setSidebarOpen(false)}
        tabIndex={sidebarOpen ? 0 : -1}
      />

      <div className={"sidebar-shell " + (sidebarOpen ? "is-open" : "")}>
        <Sidebar
          conversations={conversations}
          selectedId={selectedId}
          displayName={identity.displayName}
          locked={false}
          status={listStatus}
          hasMore={hasMore}
          loadingMore={loadingMore}
          onLoadMore={() => void loadMore()}
          onReload={retryConversations}
          onNew={() => {
            // An empty chat is already a new chat.
            if (messages.length > 0) {
              createConversation();
            }

            setSidebarOpen(false);
          }}
          onSelect={handleSelect}
          onDelete={handleDelete}
          onLogout={handleLogout}
        />
      </div>

      <section className="chat-main" aria-label="Chat workspace">
        <header className="chat-topbar">
          <button
            type="button"
            className="mobile-menu-button"
            aria-label="Open conversations"
            onClick={() => setSidebarOpen(true)}
          >
            <MenuIcon />
          </button>

          <div className="topbar-title">
            <span className="topbar-kicker">YOUR WORKSPACE</span>

            <strong>
              {conversations.find(
                (conversation) => conversation.id === selectedId,
              )?.title ?? "My Chat"}
            </strong>
          </div>

          <ConnectionStatus status={status} />
        </header>

        {apiError && (
          <ErrorBanner>
            API unreachable. Check that the orchestrator is running at the
            configured address.
          </ErrorBanner>
        )}

        {status === "offline" && (
          <ErrorBanner>
            Chat is offline. Reconnecting automatically…
          </ErrorBanner>
        )}

        {cooldownSeconds > 0 && (
          <ErrorBanner>
            {`You are sending messages too quickly. You can send again in ${cooldownSeconds}s.`}
          </ErrorBanner>
        )}

        <Toasts notices={documentNotices} onDismiss={dismissDocumentNotice} />

        <MessageList
          key={selectedId}
          messages={messages}
          isSending={isSending}
          historyStatus={historyStatus}
          hasEarlier={hasEarlier}
          loadingEarlier={loadingEarlier}
          onLoadEarlier={() => void loadEarlier()}
          onReload={retryMessages}
          onRetry={handleRetry}
          onEdit={handleEdit}
          onRegenerate={handleRegenerate}
          actionsDisabled={isSending || cooldownSeconds > 0}
          onPrompt={(prompt) => {
            setDraft(prompt);
            setSidebarOpen(false);

            window.setTimeout(
              () => document.getElementById("chat-message-input")?.focus(),
              0,
            );
          }}
        />

        <footer className="composer-area">
          <div
            ref={composerRef}
            className={"composer-wrap" + (dragging ? " is-dragging" : "")}
            onDragOver={(event) => {
              if (event.dataTransfer.types.includes("Files")) {
                event.preventDefault();
                setDragging(true);
              }
            }}
            onDragLeave={(event) => {
              if (!event.currentTarget.contains(event.relatedTarget as Node)) {
                setDragging(false);
              }
            }}
            onDrop={(event) => {
              event.preventDefault();
              setDragging(false);
              attachFiles(event.dataTransfer.files);
            }}
          >
            {docsOpen && (
              <div className="doc-popover">
                <DocumentPanel
                  documents={documents}
                  status={documentStatus}
                  selectedIds={selectedDocIds}
                  uploading={uploading}
                  error={documentError}
                  onToggle={toggleDocument}
                  onUpload={(file) => void uploadDocument(file)}
                  onRemove={(id) => void removeDocument(id)}
                  onDismissError={dismissDocumentError}
                  onReload={() => void reloadDocuments()}
                  onClose={() => setDocsOpen(false)}
                />
              </div>
            )}

            {dragging && (
              <div className="doc-drop" aria-hidden="true">
                Drop a file to upload it
              </div>
            )}

            <ChatInput
              value={draft}
              onChange={setDraft}
              onSend={() => handleSend()}
              onStop={() => cancel(selectedId)}
              sending={isSending}
              header={attachmentBar}
              tools={composerTools}
            />
          </div>

          <p className="composer-disclaimer">
            AI can make mistakes. Sources are shown as context, not verified
            facts.
          </p>
        </footer>
      </section>
    </main>
  );
}