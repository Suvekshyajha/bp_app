import { useEffect, useRef, useState } from "react";
import type { ChatMessage } from "../types";
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
  onRetry,
  onPrompt,
}: {
  messages: ChatMessage[];
  isSending: boolean;
  onRetry: (id: string) => void;
  onPrompt: (prompt: string) => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const shouldStickRef = useRef(true);
  const [hasScrolledUp, setHasScrolledUp] = useState(false);

  // Also follow the text while the last message grows during streaming
  const lastMessage = messages[messages.length - 1];
  const lastContent = lastMessage?.content;

  // Show "Thinking" only until the first token arrives
  const showTyping = isSending && lastMessage?.role !== "assistant";

  useEffect(() => {
    if (shouldStickRef.current) endRef.current?.scrollIntoView({ behavior: "auto", block: "end" });
  }, [messages.length, lastContent, isSending]);

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
        {messages.length === 0 ? (
          <section className="empty-state" aria-labelledby="welcome-title">
            <div className="empty-mark" aria-hidden="true">D</div>
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