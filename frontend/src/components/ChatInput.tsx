import { useRef } from "react";

export function ChatInput({
  value,
  onChange,
  onSend,
  disabled,
}: {
  value: string;
  onChange: (value: string) => void;
  onSend: () => void;
  disabled: boolean;
}) {
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const submit = () => {
    if (disabled || !value.trim()) return;
    onSend();
    window.requestAnimationFrame(() => inputRef.current?.focus());
  };

  return (
    <form className="chat-input-form" onSubmit={(event) => { event.preventDefault(); submit(); }}>
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
        placeholder="Write a message…"
        maxLength={4000}
        rows={1}
        disabled={disabled}
        aria-describedby="chat-input-hint"
      />
      <div className="composer-footer">
        <span id="chat-input-hint">Enter to send · Shift+Enter for a new line · 4,000 character limit</span>
        <button className="send-button" type="submit" disabled={disabled || !value.trim()} aria-label="Send message">
          <span>Send</span><span aria-hidden="true">↑</span>
        </button>
      </div>
    </form>
  );
}
