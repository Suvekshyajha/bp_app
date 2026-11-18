export function TypingIndicator() {
  return (
    <div className="typing-row" role="status" aria-label="Assistant is preparing a reply">
      <div className="assistant-avatar" aria-hidden="true">D</div>
      <div className="typing-bubble" aria-hidden="true"><span /><span /><span /></div>
      <span className="typing-label">Thinking</span>
    </div>
  );
}
