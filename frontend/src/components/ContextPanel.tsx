import type { RetrievedMessage, RetrievedPayload } from "../types";

function formatTime(value?: string): string {
  if (!value) return "Time unavailable";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Time unavailable";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

// "report.pdf · part 3" (parts are counted from 1 for people)
function documentLabel(payload: RetrievedPayload): string {
  const name = payload.filename ?? "Document";
  return typeof payload.chunk_index === "number"
    ? name + " · part " + (payload.chunk_index + 1)
    : name;
}

// "2. Results · p. 4"
function documentPlace(payload: RetrievedPayload): string {
  return [
    payload.section_title ? payload.section_title : "",
    payload.page ? "p. " + payload.page : "",
  ]
    .filter(Boolean)
    .join(" · ");
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

          // An excerpt from an uploaded document
          if (payload?.filename) {
            const place = documentPlace(payload);
            return (
              <article className="context-item context-item--document" key={key}>
                <div className="context-item-meta">
                  <strong>{documentLabel(payload)}</strong>
                  {place && <span>{place}</span>}
                </div>
                <p>{payload.content || "Content unavailable."}</p>
              </article>
            );
          }

          // A past chat message
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
