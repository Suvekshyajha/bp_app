import type { RetrievedMessage, RetrievedPayload } from "../types";

function formatTime(value?: string): string {
  if (!value) return "Time unavailable";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Time unavailable";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

// "report.pdf · part 3" (parts are counted from 1 for people, from 0 inside)
function sourceLabel(payload: RetrievedPayload): string {
  const parts = [payload.filename ?? "Document"];
  if (typeof payload.chunk_index === "number") parts.push("part " + (payload.chunk_index + 1));
  if (payload.page) parts.push("p. " + payload.page);
  return parts.join(" · ");
}

export function ContextPanel({ items }: { items: RetrievedMessage[] }) {
  if (items.length === 0) return null;
  return (
    <details className="context-panel">
      <summary>Sources <span>({items.length})</span></summary>
      <div className="context-items">
        {items.map((item, index) => {
          const payload = item.payload;
          const key = item.id ?? payload?.message_id ?? String(index);

          // An excerpt from an uploaded document names its file, part and section
          if (payload?.filename) {
            return (
              <article className="context-item is-document" key={key}>
                <div className="context-item-meta">
                  <strong>{sourceLabel(payload)}</strong>
                  {payload.section_title && <span>{payload.section_title}</span>}
                </div>
                <p>{payload.content || "Content unavailable."}</p>
              </article>
            );
          }

          const role = payload?.role === "user" ? "You" : payload?.role === "assistant" ? "Assistant" : "Related message";
          return (
            <article className="context-item" key={key}>
              <div className="context-item-meta"><strong>{role}</strong><time>{formatTime(payload?.timestamp)}</time></div>
              <p>{payload?.content || "Content unavailable."}</p>
            </article>
          );
        })}
      </div>
    </details>
  );
}
