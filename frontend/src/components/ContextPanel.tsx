import type { RetrievedMessage } from "../types";

function formatTime(value?: string): string {
  if (!value) return "Time unavailable";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Time unavailable";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

export function ContextPanel({ items }: { items: RetrievedMessage[] }) {
  if (items.length === 0) return null;
  return (
    <details className="context-panel">
      <summary>Related past messages <span>({items.length})</span></summary>
      <div className="context-items">
        {items.map((item, index) => {
          const payload = item.payload;
          const role = payload?.role === "user" ? "You" : payload?.role === "assistant" ? "Assistant" : "Related message";
          return (
            <article className="context-item" key={item.id ?? payload?.message_id ?? String(index)}>
              <div className="context-item-meta"><strong>{role}</strong><time>{formatTime(payload?.timestamp)}</time></div>
              <p>{payload?.content || "Content unavailable."}</p>
            </article>
          );
        })}
      </div>
    </details>
  );
}
