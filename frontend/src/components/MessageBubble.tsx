import { useEffect, useRef, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatMessage } from "../types";
import { ContextPanel } from "./ContextPanel";

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(date);
}

const iconProps = {
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round",
  strokeLinejoin: "round",
  "aria-hidden": true,
} as const;

const CopyIcon = () => (
  <svg {...iconProps}><rect x="9" y="9" width="11" height="11" rx="2" /><path d="M5 15V6a2 2 0 0 1 2-2h9" /></svg>
);
const CheckIcon = () => (
  <svg {...iconProps}><path d="M5 12.5l4.5 4.5L19 7.5" /></svg>
);
const EditIcon = () => (
  <svg {...iconProps}><path d="M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16v4z" /></svg>
);
const RegenerateIcon = () => (
  <svg {...iconProps}><path d="M20 12a8 8 0 1 1-2.6-5.9" /><path d="M20 4v5h-5" /></svg>
);

// navigator.clipboard only exists on https pages, so there is a fallback
async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(area);
      return ok;
    } catch {
      return false;
    }
  }
}

function CopyButton({ text }: { text: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const timerRef = useRef<number | undefined>(undefined);

  useEffect(() => () => window.clearTimeout(timerRef.current), []);

  const handleCopy = async () => {
    const ok = await copyText(text);
    setState(ok ? "copied" : "failed");
    window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(() => setState("idle"), 1500);
  };

  return (
    <button
      type="button"
      className="action-button"
      onClick={() => void handleCopy()}
      aria-label="Copy message"
      title="Copy message"
    >
      {state === "copied" ? <CheckIcon /> : <CopyIcon />}
      <span>{state === "copied" ? "Copied" : state === "failed" ? "Failed" : "Copy"}</span>
    </button>
  );
}

// Model output is untrusted. The browser loads an image on its own, with no
// click, so an image address can carry private text out. Images are never
// rendered, and a link is shown as plain text plus its real address, so the
// user sees where it goes and nothing is loaded or clickable.
const SAFE_MARKDOWN_COMPONENTS: Components = {
  img: ({ alt }) => <span>{alt ? `[image: ${alt}]` : "[image removed]"}</span>,
  a: ({ href, children }) => {
    // A bare URL (autolinked by remark-gfm) already shows its own address
    const showsAddress = typeof children === "string" && children === href;
    return (
      <span>
        {children}
        {href && !showsAddress ? ` (${href})` : ""}
      </span>
    );
  },
};

export function MessageBubble({
  message,
  onRetry,
  onEdit,
  onRegenerate,
  actionsDisabled,
  canRegenerate,
}: {
  message: ChatMessage;
  onRetry: (id: string) => void;
  onEdit: (id: string, text: string) => boolean; // true = accepted, editor closes
  onRegenerate: (id: string) => void;
  actionsDisabled: boolean;
  canRegenerate: boolean;
}) {
  const isUser = message.role === "user";

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const editRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const element = editRef.current;
    if (editing && element) {
      element.focus();
      element.setSelectionRange(element.value.length, element.value.length);
    }
  }, [editing]);

  const startEdit = () => {
    setDraft(message.content);
    setEditing(true);
  };

  const saveEdit = () => {
    const text = draft.trim();

    if (!text || actionsDisabled) return;

    if (text === message.content.trim()) {
      setEditing(false); // nothing changed
      return;
    }

    if (onEdit(message.id, text)) setEditing(false);
  };

  return (
    <article className={"message-row " + (isUser ? "message-row--user" : "message-row--assistant")}>
      {!isUser && <div className="assistant-avatar" aria-hidden="true">I</div>}
      <div className={"message-stack" + (editing ? " message-stack--editing" : "")}>
        {editing ? (
          <div className="edit-box">
            <textarea
              ref={editRef}
              value={draft}
              maxLength={4000}
              rows={Math.min(8, Math.max(2, draft.split("\n").length))}
              aria-label="Edit your message"
              onChange={(event) => setDraft(event.target.value.slice(0, 4000))}
              onKeyDown={(event) => {
                if (event.key === "Escape") setEditing(false);
                if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  saveEdit();
                }
              }}
            />
            <div className="edit-actions">
              <button type="button" className="edit-cancel" onClick={() => setEditing(false)}>Cancel</button>
              <button type="button" className="edit-save" onClick={saveEdit} disabled={!draft.trim() || actionsDisabled}>
                Save &amp; send
              </button>
            </div>
          </div>
        ) : (
          <div className={"message-bubble " + (isUser ? "message-bubble--user" : "message-bubble--assistant")}>
            {isUser ? (
              <p className="plain-message">{message.content}</p>
            ) : (
              <div className="markdown-content">
                <ReactMarkdown remarkPlugins={[remarkGfm]} components={SAFE_MARKDOWN_COMPONENTS}>
                  {message.content}
                </ReactMarkdown>
              </div>
            )}
            <div className="message-meta">
              <time dateTime={message.createdAt}>{formatTime(message.createdAt)}</time>
              {isUser && message.status === "pending" && <span>Sending</span>}
              {isUser && message.status === "sent" && <span>Sent</span>}
              {isUser && message.status === "failed" && <span className="failed-label">Not sent</span>}
            </div>
          </div>
        )}
        {isUser && message.status === "failed" && !editing && (
          <div className="message-error">
            <span>{message.error || "This message could not be sent."}</span>
            <button type="button" onClick={() => onRetry(message.id)}>Retry</button>
          </div>
        )}

        {isUser && !editing && (
          <div className="message-actions">
            <CopyButton text={message.content} />
            <button
              type="button"
              className="action-button"
              onClick={startEdit}
              disabled={actionsDisabled}
              aria-label="Edit message"
              title="Edit message"
            >
              <EditIcon /><span>Edit</span>
            </button>
          </div>
        )}

        {!isUser && canRegenerate && (
          <div className="message-actions">
            <button
              type="button"
              className="action-button"
              onClick={() => onRegenerate(message.id)}
              disabled={actionsDisabled}
              aria-label="Regenerate response"
              title="Regenerate response"
            >
              <RegenerateIcon /><span>Regenerate</span>
            </button>
          </div>
        )}

        {!isUser && <ContextPanel items={message.context ?? []} />}
      </div>
      {isUser && <div className="user-avatar" aria-hidden="true">You</div>}
    </article>
  );
}