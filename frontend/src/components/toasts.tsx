import { useEffect } from "react";
import type { Notice } from "../hooks/useDocuments";

// How long a message stays. A failure stays longer: it is worth reading twice.
const VISIBLE_MS = { success: 5000, info: 5000, error: 10000 } as const;

function Toast({
  notice,
  onDismiss,
}: {
  notice: Notice;
  onDismiss: (id: number) => void;
}) {
  const { id, kind } = notice;

  useEffect(() => {
    const timer = window.setTimeout(() => onDismiss(id), VISIBLE_MS[kind]);
    return () => window.clearTimeout(timer);
  }, [id, kind, onDismiss]);

  return (
    <div
      className={"toast is-" + kind}
      role={kind === "error" ? "alert" : "status"}
    >
      <span className="toast-icon" aria-hidden="true">
        {kind === "error" ? "!" : "✓"}
      </span>
      <span className="toast-text">{notice.message}</span>
      <button
        type="button"
        className="toast-close"
        aria-label="Dismiss notification"
        onClick={() => onDismiss(id)}
      >
        ×
      </button>
    </div>
  );
}

export function Toasts({
  notices,
  onDismiss,
}: {
  notices: Notice[];
  onDismiss: (id: number) => void;
}) {
  if (notices.length === 0) return null;

  return (
    <div className="toast-stack" aria-live="polite">
      {notices.map((notice) => (
        <Toast key={notice.id} notice={notice} onDismiss={onDismiss} />
      ))}
    </div>
  );
}