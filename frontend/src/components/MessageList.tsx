import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ChatMessage } from "../types";
import type { HistoryStatus } from "../hooks/useChatMessages";
import { MessageBubble } from "./MessageBubble";
import { TypingIndicator } from "./TypingIndicator";

const prompts = [
  "Help me turn a rough idea into a simple plan.",
  "What patterns do you notice in my past conversations?",
  "Help me write a clear follow-up message.",
];

export function MessageList({
  messages,
  isSending,
  historyStatus,
  hasEarlier,
  loadingEarlier,
  onLoadEarlier,
  onReload,
  onRetry,
  onPrompt,
}: {
  messages: ChatMessage[];
  isSending: boolean;
  historyStatus: HistoryStatus;
  hasEarlier: boolean;
  loadingEarlier: boolean;
  onLoadEarlier: () => void;
  onReload: () => void;
  onRetry: (id: string) => void;
  onPrompt: (prompt: string) => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const shouldStickRef = useRef(true);
  const [hasScrolledUp, setHasScrolledUp] = useState(false);

  // Used to keep the view still when older messages are added above it
  const firstIdRef = useRef<string | undefined>(messages[0]?.id);
  const heightBeforeLoadRef = useRef(0);

  // Also follow the text while the last message grows during streaming
  const lastMessage = messages[messages.length - 1];
  const lastContent = lastMessage?.content;

  // Show "Thinking" only until the first token arrives
  const showTyping = isSending && lastMessage?.role !== "assistant";

  useEffect(() => {
    if (shouldStickRef.current) endRef.current?.scrollIntoView({ behavior: "auto", block: "end" });
  }, [messages.length, lastContent, isSending, historyStatus]);

  // Older messages were put at the top: push the scroll position down by the
  // height that was added, so the reader stays on the same message.
  useLayoutEffect(() => {
    const element = scrollRef.current;
    const firstId = messages[0]?.id;

    if (
      element &&
      heightBeforeLoadRef.current > 0 &&
      firstIdRef.current &&
      firstId !== firstIdRef.current
    ) {
      element.scrollTo({
        top: element.scrollTop + (element.scrollHeight - heightBeforeLoadRef.current),
        behavior: "instant",
      });
      heightBeforeLoadRef.current = 0;
    }

    firstIdRef.current = firstId;
  }, [messages]);

  const handleLoadEarlier = () => {
    heightBeforeLoadRef.current = scrollRef.current?.scrollHeight ?? 0;
    onLoadEarlier();
  };

  const handleScroll = () => {
    const element = scrollRef.current;
    if (!element) return;
    const nearBottom = element.scrollHeight - element.scrollTop - element.clientHeight < 90;
    shouldStickRef.current = nearBottom;
    setHasScrolledUp(!nearBottom && element.scrollHeight > element.clientHeight);
  };

  return (
    <div className="message-list-scroll" ref={scrollRef} onScroll={handleScroll}>
      <div className="message-list" aria-live="polite" aria-relevant="additions text">
        {hasEarlier && (
          <button type="button" className="load-earlier-button" onClick={handleLoadEarlier} disabled={loadingEarlier}>
            {loadingEarlier ? "Loading…" : "Load earlier messages"}
          </button>
        )}
        {historyStatus === "loading" && messages.length === 0 ? (
          <p className="history-notice">Loading messages…</p>
        ) : historyStatus === "error" && messages.length === 0 ? (
          <p className="history-notice">
            Could not load this chat. <button type="button" className="load-more-button" onClick={onReload}>Retry</button>
          </p>
        ) : messages.length === 0 ? (
          <section className="empty-state" aria-labelledby="welcome-title">
            <div className="empty-mark" aria-hidden="true">I</div>
            <p className="eyebrow">INFOMATE</p>
            <h1 id="welcome-title">A clearer place to think.</h1>
            <p className="empty-copy">Ask a question. When it helps, related messages from your past conversations can add context.</p>
            <div className="prompt-list" aria-label="Example prompts">
              {prompts.map((prompt) => (
                <button type="button" className="prompt-chip" key={prompt} onClick={() => onPrompt(prompt)}>{prompt}<span aria-hidden="true">↗</span></button>
              ))}
            </div>
          </section>
        ) : (
          messages.map((message) => <MessageBubble key={message.id} message={message} onRetry={onRetry} />)
        )}
        {showTyping && <TypingIndicator />}
        <div ref={endRef} />
      </div>
      {hasScrolledUp && messages.length > 2 && (
        <button
          type="button"
          className="jump-latest"
          onClick={() => { shouldStickRef.current = true; endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" }); }}
        >Jump to latest ↓</button>
      )}
    </div>
  );
}