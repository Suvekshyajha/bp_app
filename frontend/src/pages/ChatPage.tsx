import { useCallback, useEffect, useRef, useState } from "react";
import type { Identity } from "../types";
import { getHealth } from "../api";
import { useChatSocket } from "../hooks/useChatSocket";
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
    createConversation,
    selectConversation,
    touchConversation,
    deleteConversation,
  } = useConversations();

  const { messages, addMessage, updateMessage, removeMessage, removeChat } =
    useChatMessages(selectedId);

  const { status, send, cancel } = useChatSocket(onLogout);

  const [draft, setDraft] = useState("");
  // Which chats are waiting for an answer. Several can wait at the same time.
  const [sendingIds, setSendingIds] = useState<ReadonlySet<string>>(new Set());
  const [apiError, setApiError] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);

  const sendingRef = useRef<Set<string>>(new Set());

  const isSending = sendingIds.has(selectedId);

  const markSending = useCallback((id: string, on: boolean) => {
    if (on) sendingRef.current.add(id);
    else sendingRef.current.delete(id);

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

  const sendExistingMessage = useCallback(
    async (query: string, messageId: string, conversationId: string) => {
      if (sendingRef.current.has(conversationId)) return;

      markSending(conversationId, true);

      updateMessage(conversationId, messageId, (message) => ({
        ...message,
        status: "pending",
        error: undefined,
      }));

      // The assistant bubble is created when the first token arrives
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
          { query, conversation_id: conversationId },
          handleToken,
        );

        updateMessage(conversationId, messageId, (message) => ({
          ...message,
          status: "sent",
          error: undefined,
        }));

        if (started) {
          // Finish the bubble we were streaming into
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
        // Drop a half-written answer so a retry does not leave a duplicate
        if (started) removeMessage(conversationId, assistantId);

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
    [addMessage, markSending, removeMessage, send, updateMessage],
  );

  const handleSend = useCallback(
    (rawText?: string) => {
      const query = (rawText ?? draft).trim().slice(0, 4000);

      if (!query || sendingRef.current.has(selectedId) || !selectedId) return;

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
    [addMessage, draft, selectedId, sendExistingMessage, touchConversation],
  );

  const handleRetry = useCallback(
    (messageId: string) => {
      const failedMessage = messages.find(
        (message) => message.id === messageId,
      );

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
    [messages, selectedId, sendExistingMessage, touchConversation],
  );

  const handleLogout = () => {
    const confirmed = window.confirm("Are you sure you want to log out?");

    if (confirmed) onLogout();
  };

  const handleDelete = (id: string) => {
    const confirmed = window.confirm(
      "Delete this chat from this browser? Your saved memory on the server is kept.",
    );

    if (confirmed) {
      cancel(id); // stop its answer if one is being written
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
          onNew={() => {
            // An empty chat is already a "new chat": do not pile up blank ones
            if (messages.length > 0) createConversation();
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

        <MessageList
          messages={messages}
          isSending={isSending}
          onRetry={handleRetry}
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