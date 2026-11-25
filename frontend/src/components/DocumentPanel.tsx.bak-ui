import { useRef } from "react";
import type { DocumentInfo } from "../types";
import type { DocumentListStatus } from "../hooks/useDocuments";
import { STATUS_LABELS, describeDocumentError } from "./documentStatus";
import {
  ACCEPTED_EXTENSIONS,
  MAX_SELECTED_DOCUMENTS,
  MAX_UPLOAD_MB,
} from "../services/documentLimits";

export function DocumentPanel({
  documents,
  status,
  selectedIds,
  uploading,
  error,
  onToggle,
  onUpload,
  onRemove,
  onDismissError,
  onReload,
}: {
  documents: DocumentInfo[];
  status: DocumentListStatus;
  selectedIds: readonly string[];
  uploading: boolean;
  error: string | null;
  onToggle: (id: string) => void;
  onUpload: (file: File) => void;
  onRemove: (id: string) => void;
  onDismissError: () => void;
  onReload: () => void;
}) {
  const pickerRef = useRef<HTMLInputElement>(null);
  const limitReached = selectedIds.length >= MAX_SELECTED_DOCUMENTS;

  return (
    <details className="doc-panel">
      <summary>
        Documents <span>({documents.length})</span>
        {selectedIds.length > 0 && (
          <span className="doc-selected-count">
            {selectedIds.length} selected for questions
          </span>
        )}
      </summary>

      <div className="doc-body">
        <div className="doc-toolbar">
          <input
            ref={pickerRef}
            id="document-file-input"
            className="sr-only"
            type="file"
            accept={ACCEPTED_EXTENSIONS.join(",")}
            onChange={(event) => {
              const file = event.target.files?.[0];
              // Allow picking the same file again later
              event.target.value = "";
              if (file) onUpload(file);
            }}
          />
          <button
            type="button"
            className="doc-upload-button"
            disabled={uploading}
            onClick={() => pickerRef.current?.click()}
          >
            {uploading ? "Uploading…" : "＋ Upload a document"}
          </button>
          <span className="doc-hint">
            {ACCEPTED_EXTENSIONS.join(" ")} · up to {MAX_UPLOAD_MB} MB
          </span>
        </div>

        {error && (
          <p className="doc-error" role="alert">
            {error}{" "}
            <button type="button" className="doc-link" onClick={onDismissError}>
              Dismiss
            </button>
          </p>
        )}

        {status === "error" && (
          <p className="doc-error" role="alert">
            Could not load your documents.{" "}
            <button type="button" className="doc-link" onClick={onReload}>
              Try again
            </button>
          </p>
        )}

        {status === "loading" && <p className="doc-hint">Loading…</p>}

        {status === "ready" && documents.length === 0 && (
          <p className="doc-hint">
            No documents yet. Upload one, then tick it to ask about it.
          </p>
        )}

        <ul className="doc-list">
          {documents.map((document) => {
            const ready = document.status === "ready";
            const selected = selectedIds.includes(document.id);
            // A tick is only offered for a ready document; the 6th is refused
            const canTick = ready && (selected || !limitReached);

            return (
              <li className="doc-item" key={document.id}>
                <label className="doc-check">
                  <input
                    type="checkbox"
                    checked={selected}
                    disabled={!canTick}
                    onChange={() => onToggle(document.id)}
                    aria-label={"Ask about " + document.filename}
                  />
                </label>

                <div className="doc-info">
                  <span className="doc-name" title={document.filename}>
                    {document.filename}
                  </span>

                  <span className={"doc-badge is-" + document.status}>
                    {STATUS_LABELS[document.status]}
                  </span>

                  {document.status === "failed" && (
                    <span className="doc-failure">
                      {describeDocumentError(document.error)}
                    </span>
                  )}
                </div>

                <button
                  type="button"
                  className="doc-delete"
                  aria-label={"Delete " + document.filename}
                  title="Delete document"
                  disabled={document.status === "deleting"}
                  onClick={() => onRemove(document.id)}
                >
                  ×
                </button>
              </li>
            );
          })}
        </ul>

        {limitReached && (
          <p className="doc-hint">
            You can ask about up to {MAX_SELECTED_DOCUMENTS} documents at once.
          </p>
        )}
      </div>
    </details>
  );
}
