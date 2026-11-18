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

  const { messages, addMessage, updateMessage } = useChatMessages(selectedId);

  const { status, send } = useChatSocket(identity.token, onLogout);

  const [draft, setDraft] = useState("");
  const [isSending, setIsSending] = useState(false);
  const [apiError, setApiError] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);

  const sendingRef = useRef(false);

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
      if (sendingRef.current) return;

      sendingRef.current = true;
      setIsSending(true);

      updateMessage(messageId, (message) => ({
        ...message,
        status: "pending",
        error: undefined,
      }));

      try {
        const response = await send({ query });

        updateMessage(messageId, (message) => ({
          ...message,
          status: "sent",
          error: undefined,
        }));

        addMessage({
          id: crypto.randomUUID(),
          role: "assistant",
          content: response.answer,
          createdAt: new Date().toISOString(),
          context: response.results,
        });
      } catch (error) {
        const message =
          error instanceof Error
            ? error.message
            : "Something went wrong while sending your message.";

        updateMessage(messageId, (current) => ({
          ...current,
          status: "failed",
          error: message,
        }));
      } finally {
        sendingRef.current = false;
        setIsSending(false);
      }
    },
    [addMessage, send, updateMessage],
  );

  const handleSend = useCallback(
    (rawText?: string) => {
      const query = (rawText ?? draft).trim().slice(0, 4000);

      if (!query || sendingRef.current || !selectedId) return;

      const messageId = crypto.randomUUID();
      const conversationId = selectedId;

      addMessage({
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
          locked={isSending}
          onNew={() => {
            createConversation();
            setSidebarOpen(false);
          }}
          onSelect={handleSelect}
          onDelete={deleteConversation}
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
            disabled={isSending}
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