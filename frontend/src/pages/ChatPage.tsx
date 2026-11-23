
import { useCallback, useEffect, useRef, useState } from "react";
import type { Identity } from "../types";
import { getHealth } from "../api";
import { RateLimitError, useChatSocket } from "../hooks/useChatSocket";
import { useConversations } from "../hooks/useConversations";
import { useChatMessages } from "../hooks/useChatMessages";
import { Sidebar } from "../components/Sidebar";
import { ConnectionStatus } from "../components/ConnectionStatus";
import { ErrorBanner } from "../components/ErrorBanner";
import { MessageList } from "../components/MessageList";
import { ChatInput } from "../components/ChatInput";

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

  const [draft, setDraft] = useState("");

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
  const sendExistingMessage = useCallback(
    async (query: string, messageId: string, conversationId: string) => {
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

      // Generate a fresh assistant response.
      void sendExistingMessage(
        userMessage.content,
        userMessage.id,
        selectedId,
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
            ☰
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
          <ChatInput
            value={draft}
            onChange={setDraft}
            onSend={() => handleSend()}
            onStop={() => cancel(selectedId)}
            sending={isSending}
          />

          <p className="composer-disclaimer">
            AI can make mistakes. Related past messages are shown as context,
            not verified facts.
          </p>
        </footer>
      </section>
    </main>
  );
}
