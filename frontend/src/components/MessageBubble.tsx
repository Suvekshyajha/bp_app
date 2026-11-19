import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatMessage } from "../types";
import { ContextPanel } from "./ContextPanel";

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(date);
}

export function MessageBubble({ message, onRetry }: { message: ChatMessage; onRetry: (id: string) => void }) {
  const isUser = message.role === "user";
  return (
    <article className={"message-row " + (isUser ? "message-row--user" : "message-row--assistant")}>
      {!isUser && <div className="assistant-avatar" aria-hidden="true">I</div>}
      <div className="message-stack">
        <div className={"message-bubble " + (isUser ? "message-bubble--user" : "message-bubble--assistant")}>
          {isUser ? (
            <p className="plain-message">{message.content}</p>
          ) : (
            <div className="markdown-content"><ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown></div>
          )}
          <div className="message-meta">
            <time dateTime={message.createdAt}>{formatTime(message.createdAt)}</time>
            {isUser && message.status === "pending" && <span>Sending</span>}
            {isUser && message.status === "sent" && <span>Sent</span>}
            {isUser && message.status === "failed" && <span className="failed-label">Not sent</span>}
          </div>
        </div>
        {isUser && message.status === "failed" && (
          <div className="message-error">
            <span>{message.error || "This message could not be sent."}</span>
            <button type="button" onClick={() => onRetry(message.id)}>Retry</button>
          </div>
        )}
        {!isUser && <ContextPanel items={message.context ?? []} />}
      </div>
      {isUser && <div className="user-avatar" aria-hidden="true">You</div>}
    </article>
  );
}
