import { useRef, type ReactNode } from "react";
import { ArrowUpIcon, StopIcon } from "./Icons";

export function ChatInput({
  value,
  onChange,
  onSend,
  onStop,
  sending,
  header,
  tools,
}: {
  value: string;
  onChange: (value: string) => void;
  onSend: () => void;
  onStop?: () => void;
  sending: boolean;
  // Shown inside the box, above the text (attached documents)
  header?: ReactNode;
  // Buttons at the bottom-left of the box (attach, documents)
  tools?: ReactNode;
}) {
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const submit = () => {
    if (sending || !value.trim()) return;
    onSend();
    window.requestAnimationFrame(() => inputRef.current?.focus());
  };

  return (
    <form className="chat-input-form" onSubmit={(event) => { event.preventDefault(); submit(); }}>
      {header}
      <label className="sr-only" htmlFor="chat-message-input">Message the assistant</label>
      <textarea
        ref={inputRef}
        id="chat-message-input"
        value={value}
        onChange={(event) => onChange(event.target.value.slice(0, 4000))}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
            event.preventDefault();
            submit();
          }
        }}
        placeholder={sending ? "Type your next message while the answer is written…" : "Ask a question, or attach a document…"}
        maxLength={4000}
        rows={1}
        aria-describedby="chat-input-hint"
      />
      <div className="composer-footer">
        <div className="composer-tools">
          {tools}
          <span id="chat-input-hint">Enter to send · Shift+Enter for a new line · 4,000 character limit</span>
        </div>
        {sending && onStop ? (
          <button className="send-button" type="button" onClick={onStop} aria-label="Stop generating">
            <span>Stop</span><StopIcon />
          </button>
        ) : (
          <button className="send-button" type="submit" disabled={sending || !value.trim()} aria-label="Send message">
            <span>Send</span><ArrowUpIcon />
          </button>
        )}
      </div>
    </form>
  );
}
