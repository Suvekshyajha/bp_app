#!/usr/bin/env python3
"""
add_document_feature.py
=======================

Restores the missing "Documents" feature in the React frontend.

What was wrong
--------------
* src/components/DocumentPanel.tsx  -> EMPTY file (0 bytes)
* src/hooks/useDocuments.ts         -> EMPTY file (0 bytes)
* src/api.ts                        -> no getDocuments / uploadDocument / deleteDocument
* src/types.ts                      -> no DocumentInfo / DocumentStatus, no document_ids
* src/components/ContextPanel.tsx   -> still the old "Related past messages" view
                                       (the tests expect "Sources" + document excerpts)
* src/pages/ChatPage.tsx            -> never uses the document panel, never sends document_ids
* src/index.css                     -> no styles for any of it

What this script does
---------------------
Writes / patches the files above so that:
  - a "Documents" button appears in the chat top bar and opens a side panel
  - you can upload .pdf .docx .txt .md files, see their status (Queued, Processing,
    Ready, Failed), and delete them
  - ready documents can be ticked (max 5); the ticked ones are sent with every
    message as `document_ids`, and shown as chips above the message box
  - the answer's "Sources" list shows document excerpts (file name, part, page)

Usage
-----
    python add_document_feature.py                    # auto-finds the frontend folder
    python add_document_feature.py path/to/frontend   # or point at it
    python add_document_feature.py --dry-run          # show what would change

Safe to re-run: every changed file is backed up once as <file>.bak-docs and
already-patched files are skipped. CRLF / LF line endings are preserved.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

MARK_CSS = "/* ==== document feature ==== */"

# --------------------------------------------------------------------------
# New / replacement file contents
# --------------------------------------------------------------------------

USE_DOCUMENTS_TS = r'''import { useCallback, useEffect, useRef, useState } from "react";
import {
  NotFoundError,
  UnauthorizedError,
  deleteDocument,
  getDocuments,
  uploadDocument,
} from "../api";
import { isBusy } from "../components/documentStatus";
import { checkFile } from "../services/documentLimits";
import type { DocumentInfo } from "../types";

// While a document is Queued or Processing the list is refreshed this often
export const POLL_INTERVAL_MS = 2000;

export type DocumentListStatus = "loading" | "ready" | "error";

function newestFirst(list: DocumentInfo[]): DocumentInfo[] {
  const time = (value: string) => Date.parse(value) || 0;
  return [...list].sort((a, b) => time(b.createdAt) - time(a.createdAt));
}

function messageOf(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

export function useDocuments(onAuthError?: () => void) {
  const [documents, setDocuments] = useState<DocumentInfo[]>([]);
  const [status, setStatus] = useState<DocumentListStatus>("loading");
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onAuthErrorRef = useRef(onAuthError);

  useEffect(() => {
    onAuthErrorRef.current = onAuthError;
  }, [onAuthError]);

  const fetchList = useCallback((isActive: () => boolean) => {
    return getDocuments()
      .then((list) => {
        if (!isActive()) return;
        setDocuments(newestFirst(list));
        setStatus("ready");
      })
      .catch((failure: unknown) => {
        if (!isActive()) return;

        if (failure instanceof UnauthorizedError) {
          onAuthErrorRef.current?.();
        } else {
          setStatus("error");
        }
      });
  }, []);

  // First load
  useEffect(() => {
    let active = true;
    void fetchList(() => active);

    return () => {
      active = false;
    };
  }, [fetchList]);

  const reload = useCallback(() => {
    setStatus("loading");
    void fetchList(() => true);
  }, [fetchList]);

  // Keep refreshing while the server is still working on a document,
  // and stop as soon as every document has settled.
  const hasBusy = documents.some((document) => isBusy(document.status));

  useEffect(() => {
    if (!hasBusy) return;

    let cancelled = false;
    let timer: number | undefined;

    const schedule = () => {
      timer = window.setTimeout(async () => {
        try {
          const list = await getDocuments();
          if (!cancelled) setDocuments(newestFirst(list));
        } catch (failure) {
          if (failure instanceof UnauthorizedError) {
            if (!cancelled) onAuthErrorRef.current?.();
            return;
          }
          // Any other failure: keep the list and try again next time
        }

        if (!cancelled) schedule();
      }, POLL_INTERVAL_MS);
    };

    schedule();

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [hasBusy]);

  const upload = useCallback(async (file: File) => {
    const problem = checkFile(file);

    if (problem) {
      setError(problem);
      return;
    }

    setError(null);
    setUploading(true);

    try {
      const created = await uploadDocument(file);

      // The same file twice gives the same id: keep one entry
      setDocuments((current) =>
        newestFirst([created, ...current.filter((d) => d.id !== created.id)]),
      );
    } catch (failure) {
      if (failure instanceof UnauthorizedError) {
        onAuthErrorRef.current?.();
      } else {
        setError(messageOf(failure, "The upload failed."));
      }
    } finally {
      setUploading(false);
    }
  }, []);

  const remove = useCallback(async (id: string) => {
    setError(null);

    try {
      await deleteDocument(id);
      setDocuments((current) => current.filter((d) => d.id !== id));
    } catch (failure) {
      if (failure instanceof NotFoundError) {
        // Already gone on the server
        setDocuments((current) => current.filter((d) => d.id !== id));
      } else if (failure instanceof UnauthorizedError) {
        onAuthErrorRef.current?.();
      } else {
        setError(messageOf(failure, "The document could not be deleted."));
      }
    }
  }, []);

  const dismissError = useCallback(() => setError(null), []);

  return {
    documents,
    status,
    uploading,
    error,
    upload,
    remove,
    reload,
    dismissError,
  };
}
'''

DOCUMENT_PANEL_TSX = r'''import { useRef } from "react";
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
  selectedIds: string[];
  uploading: boolean;
  error: string | null;
  onToggle: (id: string) => void;
  onUpload: (file: File) => void;
  onRemove: (id: string) => void;
  onDismissError: () => void;
  onReload: () => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const limitReached = selectedIds.length >= MAX_SELECTED_DOCUMENTS;

  return (
    <section className="document-panel" aria-label="Your documents">
      <div className="document-upload">
        <input
          id="document-file-input"
          ref={inputRef}
          className="sr-only"
          type="file"
          accept={ACCEPTED_EXTENSIONS.join(",")}
          tabIndex={-1}
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) onUpload(file);
            // Allow choosing the same file again
            event.target.value = "";
          }}
        />

        <button
          type="button"
          className="document-upload-button"
          disabled={uploading}
          onClick={() => inputRef.current?.click()}
        >
          {uploading ? "Uploading…" : "Upload document"}
        </button>

        <p className="document-hint">
          {"PDF, DOCX, TXT or MD · up to " + MAX_UPLOAD_MB + " MB"}
        </p>
      </div>

      {error && (
        <div className="document-error" role="alert">
          <span>{error}</span>
          <button type="button" aria-label="Dismiss" onClick={onDismissError}>
            ×
          </button>
        </div>
      )}

      {status === "loading" && (
        <p className="document-note">Loading documents…</p>
      )}

      {status === "error" && (
        <p className="document-note">
          Could not load your documents.{" "}
          <button type="button" className="document-link" onClick={onReload}>
            Try again
          </button>
        </p>
      )}

      {status === "ready" && documents.length === 0 && (
        <p className="document-note">
          No documents yet. Upload a file, then tick it to ask about it.
        </p>
      )}

      {documents.length > 0 && (
        <>
          <p className="document-count">
            {selectedIds.length +
              " of " +
              MAX_SELECTED_DOCUMENTS +
              " selected for your next question"}
          </p>

          <ul className="document-list">
            {documents.map((doc) => {
              const selected = selectedIds.includes(doc.id);
              const canTick =
                doc.status === "ready" && (selected || !limitReached);

              return (
                <li
                  key={doc.id}
                  className={"document-item" + (selected ? " is-selected" : "")}
                >
                  <input
                    type="checkbox"
                    aria-label={"Ask about " + doc.filename}
                    checked={selected}
                    disabled={!canTick}
                    onChange={() => onToggle(doc.id)}
                  />

                  <div className="document-main">
                    <span className="document-name" title={doc.filename}>
                      {doc.filename}
                    </span>

                    <span
                      className={"document-status document-status--" + doc.status}
                    >
                      {STATUS_LABELS[doc.status]}
                    </span>

                    {doc.status === "failed" && (
                      <p className="document-failure">
                        {describeDocumentError(doc.error)}
                      </p>
                    )}
                  </div>

                  <button
                    type="button"
                    className="document-delete"
                    aria-label={"Delete " + doc.filename}
                    title="Delete document"
                    disabled={doc.status === "deleting"}
                    onClick={() => onRemove(doc.id)}
                  >
                    ×
                  </button>
                </li>
              );
            })}
          </ul>
        </>
      )}
    </section>
  );
}
'''

CONTEXT_PANEL_TSX = r'''import type { RetrievedMessage, RetrievedPayload } from "../types";

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
'''

TYPES_APPEND = r'''
// ---- Documents (backend/ingestion, through /api/documents) ----
export type DocumentStatus =
  | "pending"
  | "processing"
  | "ready"
  | "failed"
  | "deleting";

export interface DocumentInfo {
  id: string;
  filename: string;
  status: DocumentStatus;
  stage?: string;
  // A short safe code such as "no_text" (see components/documentStatus.ts)
  error?: string;
  chunkCount?: number;
  createdAt: string;
}

export interface ServerDocument {
  id: string;
  filename: string;
  status: DocumentStatus;
  stage?: string | null;
  error?: string | null;
  chunk_count?: number | null;
  created_at: string | null;
}
'''

API_APPEND = r'''

// ---- Documents -------------------------------------------------------

function toDocument(item: ServerDocument): DocumentInfo {
  return {
    id: item.id,
    filename: item.filename,
    status: item.status,
    stage: item.stage ?? undefined,
    error: item.error ?? undefined,
    chunkCount: item.chunk_count ?? undefined,
    createdAt: item.created_at ?? new Date(0).toISOString(),
  };
}

// Turns a failed response into an Error. The server explains itself in
// {"detail": "..."}; that text is meant for the user ("File is larger than ...").
async function documentError(response: Response): Promise<Error> {
  if (response.status === 401) return new UnauthorizedError();
  if (response.status === 404) return new NotFoundError();

  let detail = "";

  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") detail = body.detail;
  } catch {
    // not JSON: use the generic text below
  }

  return new Error(detail || "HTTP " + response.status);
}

// All of the user's documents (the server answers with a plain list)
export async function getDocuments(): Promise<DocumentInfo[]> {
  const list = await getJson<ServerDocument[]>("/api/documents");
  return list.map(toDocument);
}

// Sends one file. The browser adds the multipart header and Content-Length.
export async function uploadDocument(file: File): Promise<DocumentInfo> {
  const form = new FormData();
  form.append("file", file);

  const response = await fetch(API_BASE_URL + "/api/documents", {
    method: "POST",
    credentials: "include",
    body: form,
  });

  if (!response.ok) throw await documentError(response);

  return toDocument((await response.json()) as ServerDocument);
}

export async function deleteDocument(id: string): Promise<void> {
  const response = await fetch(
    API_BASE_URL + "/api/documents/" + encodeURIComponent(id),
    { method: "DELETE", credentials: "include" },
  );

  if (!response.ok) throw await documentError(response);
}
'''

CSS_APPEND = r'''
/* ==== document feature ==== */
.documents-toggle { display: inline-flex; align-items: center; gap: 6px; margin-left: auto; padding: 7px 11px; border: 1px solid #e3e1f2; border-radius: 20px; background: #fff; color: #5b50c5; cursor: pointer; font-size: 10px; font-weight: 700; white-space: nowrap; }
.documents-toggle:hover, .documents-toggle[aria-expanded="true"] { background: var(--accent-soft); }

.document-shell { display: none; z-index: 5; width: 320px; flex: 0 0 320px; flex-direction: column; border-left: 1px solid #e8e7ef; background: var(--sidebar); }
.document-shell.is-open { display: flex; }
.document-shell-head { display: flex; align-items: center; justify-content: space-between; height: 68px; flex: 0 0 68px; padding: 0 16px; border-bottom: 1px solid #e8e7ef; font-size: 12px; }
.document-shell-close { display: grid; width: 28px; height: 28px; place-items: center; border: 0; border-radius: 7px; background: transparent; color: #77758c; cursor: pointer; font-size: 18px; }
.document-shell-close:hover { background: #e9e8f3; }

.document-panel { display: flex; flex: 1; flex-direction: column; gap: 10px; min-height: 0; overflow-y: auto; padding: 16px; }
.document-upload { display: grid; gap: 6px; }
.document-upload-button { padding: 10px 12px; border: 1px dashed #b9b3ef; border-radius: 10px; background: #fff; color: var(--accent-dark); cursor: pointer; font-size: 11px; font-weight: 700; }
.document-upload-button:hover:not(:disabled) { background: var(--accent-soft); }
.document-hint, .document-note, .document-count { margin: 0; color: var(--muted); font-size: 10px; line-height: 1.5; }
.document-link { padding: 0; border: 0; background: none; color: var(--accent-dark); cursor: pointer; font-size: inherit; font-weight: 700; text-decoration: underline; }
.document-error { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; padding: 8px 10px; border: 1px solid #eed0d0; border-radius: 9px; background: #fff7f7; color: #a14545; font-size: 10px; line-height: 1.5; }
.document-error button { border: 0; background: none; color: inherit; cursor: pointer; font-size: 14px; line-height: 1; }

.document-list { display: grid; gap: 8px; margin: 0; padding: 0; list-style: none; }
.document-item { display: flex; align-items: flex-start; gap: 9px; padding: 10px; border: 1px solid #e6e5ef; border-radius: 10px; background: #fff; }
.document-item.is-selected { border-color: #b9b3ef; background: #faf9ff; }
.document-item input[type="checkbox"] { margin-top: 2px; accent-color: var(--accent); }
.document-main { display: flex; min-width: 0; flex: 1; flex-direction: column; align-items: flex-start; gap: 5px; }
.document-name { max-width: 100%; overflow: hidden; color: #2f2e43; font-size: 11px; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
.document-status { padding: 2px 7px; border-radius: 10px; background: #efeff5; color: #6d6c80; font-size: 9px; font-weight: 700; }
.document-status--ready { background: #e6f5ee; color: var(--green); }
.document-status--processing, .document-status--deleting { background: #fbf3df; color: #8a6a27; }
.document-status--failed { background: #fbe9e9; color: var(--red); }
.document-failure { margin: 0; color: #a14545; font-size: 10px; line-height: 1.5; }
.document-delete { display: grid; width: 22px; height: 22px; flex: 0 0 22px; place-items: center; border: 0; border-radius: 6px; background: transparent; color: #9a99aa; cursor: pointer; font-size: 15px; }
.document-delete:hover:not(:disabled) { background: #fbe9e9; color: var(--red); }

.document-chips { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; width: min(760px, 100%); margin: 0 auto 8px; color: var(--muted); font-size: 10px; }
.document-chip { display: inline-flex; max-width: 220px; align-items: center; gap: 4px; padding: 3px 4px 3px 9px; overflow: hidden; border: 1px solid #d8d3fb; border-radius: 14px; background: var(--accent-soft); color: var(--accent-dark); font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
.document-chip button { border: 0; background: none; color: inherit; cursor: pointer; font-size: 13px; line-height: 1; }
.context-item--document { border-color: #dcd8fa; background: #faf9ff; }

@media (max-width: 760px) {
  .document-shell { position: fixed; inset: 0 0 0 auto; width: 320px; max-width: calc(100vw - 40px); box-shadow: -16px 0 40px #28233a16; }
  .document-shell-head { height: 60px; flex-basis: 60px; }
  .chat-topbar .documents-toggle { margin-left: auto; }
  .chat-topbar .connection-status { margin-left: 0; }
}
'''

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


class PatchError(Exception):
    pass


def read_text(path: Path) -> tuple[str, str]:
    """Returns (text with \\n endings, original newline style)."""
    raw = path.read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n"), newline


def write_text(path: Path, text: str, newline: str, dry: bool) -> None:
    if dry:
        return
    data = text.replace("\n", newline).encode("utf-8")
    path.write_bytes(data)


def backup(path: Path, dry: bool) -> None:
    target = path.with_name(path.name + ".bak-docs")
    if path.exists() and path.stat().st_size > 0 and not target.exists() and not dry:
        shutil.copy2(path, target)


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise PatchError(
            f"{label}: expected to find the anchor text exactly once, found {count}. "
            "The file differs from the version this script was written for."
        )
    return text.replace(old, new)


def log(action: str, path: Path, root: Path) -> None:
    print(f"  {action:<9} {path.relative_to(root)}")


# --------------------------------------------------------------------------
# the patches
# --------------------------------------------------------------------------


def write_new_file(path: Path, content: str, root: Path, dry: bool, force: bool) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if existing.strip() and not force:
        if existing.strip() == content.strip():
            log("ok", path, root)
            return
        print(f"  SKIPPED   {path.relative_to(root)} (not empty; use --force to overwrite)")
        return
    backup(path, dry)
    if not dry:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    log("written", path, root)


def patch_types(path: Path, root: Path, dry: bool) -> None:
    text, nl = read_text(path)
    if "DocumentInfo" in text:
        return log("ok", path, root)

    text = replace_once(
        text,
        "  timestamp?: string;\n}\n",
        "  timestamp?: string;\n"
        "  // Set only for an excerpt from an uploaded document\n"
        "  document_id?: string;\n"
        "  filename?: string;\n"
        "  chunk_index?: number;\n"
        "  section_title?: string;\n"
        "  page?: number;\n"
        "}\n",
        "types.ts RetrievedPayload",
    )
    text = replace_once(
        text,
        "  regenerate?: boolean; // true = a new answer to the same question\n",
        "  regenerate?: boolean; // true = a new answer to the same question\n"
        "  document_ids?: string[]; // uploaded documents to search (max 5)\n",
        "types.ts ChatRequest",
    )
    text = text.rstrip("\n") + "\n" + TYPES_APPEND
    backup(path, dry)
    write_text(path, text, nl, dry)
    log("patched", path, root)


def patch_api(path: Path, root: Path, dry: bool) -> None:
    text, nl = read_text(path)
    if "getDocuments" in text:
        return log("ok", path, root)

    text = replace_once(
        text,
        'import type { Page, ServerConversation, ServerMessage } from "./types";',
        "import type {\n"
        "  DocumentInfo,\n"
        "  Page,\n"
        "  ServerConversation,\n"
        "  ServerDocument,\n"
        "  ServerMessage,\n"
        '} from "./types";',
        "api.ts import",
    )
    text = text.rstrip("\n") + "\n" + API_APPEND
    backup(path, dry)
    write_text(path, text, nl, dry)
    log("patched", path, root)


def patch_css(path: Path, root: Path, dry: bool) -> None:
    text, nl = read_text(path)
    if MARK_CSS in text:
        return log("ok", path, root)
    text = text.rstrip("\n") + "\n" + CSS_APPEND
    backup(path, dry)
    write_text(path, text, nl, dry)
    log("patched", path, root)


def patch_context_panel(path: Path, root: Path, dry: bool) -> None:
    text, nl = read_text(path)
    if "documentLabel" in text:
        return log("ok", path, root)
    backup(path, dry)
    write_text(path, CONTEXT_PANEL_TSX, nl, dry)
    log("rewritten", path, root)


def patch_chat_page(path: Path, root: Path, dry: bool) -> None:
    text, nl = read_text(path)
    if "useDocuments" in text:
        return log("ok", path, root)

    # 1. imports
    text = replace_once(
        text,
        'import { useCallback, useEffect, useRef, useState } from "react";',
        'import { useCallback, useEffect, useMemo, useRef, useState } from "react";',
        "ChatPage react import",
    )
    text = replace_once(
        text,
        'import { ChatInput } from "../components/ChatInput";\n',
        'import { ChatInput } from "../components/ChatInput";\n'
        'import { DocumentPanel } from "../components/DocumentPanel";\n'
        'import { useDocuments } from "../hooks/useDocuments";\n'
        'import { MAX_SELECTED_DOCUMENTS } from "../services/documentLimits";\n',
        "ChatPage imports",
    )

    # 2. state, right after the socket hook
    text = replace_once(
        text,
        "  const { status, send, cancel } = useChatSocket(onLogout);\n",
        """  const { status, send, cancel } = useChatSocket(onLogout);

  // ---- Documents ----------------------------------------------------
  const {
    documents,
    status: documentsStatus,
    uploading,
    error: documentError,
    upload: uploadDocumentFile,
    remove: removeDocument,
    reload: reloadDocuments,
    dismissError: dismissDocumentError,
  } = useDocuments(onLogout);

  const [documentsOpen, setDocumentsOpen] = useState(false);
  const [pickedIds, setPickedIds] = useState<string[]>([]);

  // Only documents that still exist and are ready can be asked about
  const selectedDocumentIds = useMemo(
    () =>
      pickedIds.filter((id) =>
        documents.some(
          (document) => document.id === id && document.status === "ready",
        ),
      ),
    [pickedIds, documents],
  );

  // Read by sendExistingMessage so every send, retry, edit and regenerate
  // uses the current selection.
  const selectedDocumentIdsRef = useRef<string[]>([]);

  useEffect(() => {
    selectedDocumentIdsRef.current = selectedDocumentIds;
  }, [selectedDocumentIds]);

  const toggleDocument = useCallback(
    (id: string) => {
      setPickedIds(
        selectedDocumentIds.includes(id)
          ? selectedDocumentIds.filter((other) => other !== id)
          : selectedDocumentIds.length >= MAX_SELECTED_DOCUMENTS
            ? selectedDocumentIds
            : [...selectedDocumentIds, id],
      );
    },
    [selectedDocumentIds],
  );
""",
        "ChatPage document state",
    )

    # 3. send the selection with the question
    text = replace_once(
        text,
        "            user_message_id: messageId,\n"
        "            regenerate,\n"
        "          },\n",
        "            user_message_id: messageId,\n"
        "            regenerate,\n"
        "            ...(selectedDocumentIdsRef.current.length > 0\n"
        "              ? { document_ids: selectedDocumentIdsRef.current }\n"
        "              : {}),\n"
        "          },\n",
        "ChatPage send document_ids",
    )

    # 4. delete confirmation
    text = replace_once(
        text,
        "  const handleSelect = (id: string) => {\n",
        """  const handleRemoveDocument = (id: string) => {
    const name =
      documents.find((document) => document.id === id)?.filename ??
      "this document";

    const confirmed = window.confirm(
      `Delete "${name}"? It will no longer be used in answers.`,
    );

    if (confirmed) {
      void removeDocument(id);
    }
  };

  const handleSelect = (id: string) => {
""",
        "ChatPage handleRemoveDocument",
    )

    # 5. top bar button
    text = replace_once(
        text,
        "          <ConnectionStatus status={status} />\n",
        """          <button
            type="button"
            className="documents-toggle"
            aria-expanded={documentsOpen}
            aria-controls="document-shell"
            onClick={() => setDocumentsOpen((open) => !open)}
          >
            <span aria-hidden="true">📄</span>
            {selectedDocumentIds.length > 0
              ? `Documents (${selectedDocumentIds.length})`
              : "Documents"}
          </button>

          <ConnectionStatus status={status} />
""",
        "ChatPage topbar button",
    )

    # 6. chips above the message box
    text = replace_once(
        text,
        "          <ChatInput\n",
        """          {selectedDocumentIds.length > 0 && (
            <div
              className="document-chips"
              aria-label="Documents used for the next answer"
            >
              <span>Asking about:</span>

              {selectedDocumentIds.map((id) => {
                const name =
                  documents.find((document) => document.id === id)?.filename ??
                  "document";

                return (
                  <span className="document-chip" key={id} title={name}>
                    {name}
                    <button
                      type="button"
                      aria-label={"Stop using " + name}
                      onClick={() => toggleDocument(id)}
                    >
                      ×
                    </button>
                  </span>
                );
              })}
            </div>
          )}

          <ChatInput
""",
        "ChatPage chips",
    )

    # 7. the side panel
    text = replace_once(
        text,
        "        </footer>\n      </section>\n    </main>\n",
        """        </footer>
      </section>

      <aside
        id="document-shell"
        className={"document-shell " + (documentsOpen ? "is-open" : "")}
        aria-label="Documents"
      >
        <div className="document-shell-head">
          <strong>Documents</strong>

          <button
            type="button"
            className="document-shell-close"
            aria-label="Close documents"
            onClick={() => setDocumentsOpen(false)}
          >
            ×
          </button>
        </div>

        <DocumentPanel
          documents={documents}
          status={documentsStatus}
          selectedIds={selectedDocumentIds}
          uploading={uploading}
          error={documentError}
          onToggle={toggleDocument}
          onUpload={(file) => void uploadDocumentFile(file)}
          onRemove={handleRemoveDocument}
          onDismissError={dismissDocumentError}
          onReload={reloadDocuments}
        />
      </aside>
    </main>
""",
        "ChatPage panel",
    )

    backup(path, dry)
    write_text(path, text, nl, dry)
    log("patched", path, root)


# --------------------------------------------------------------------------


def find_frontend(arg: str | None) -> Path:
    candidates = [Path(arg)] if arg else [Path("."), Path("frontend"), Path("test/frontend")]
    for candidate in candidates:
        if (candidate / "src" / "pages" / "ChatPage.tsx").is_file():
            return candidate.resolve()
    raise SystemExit(
        "Could not find the frontend folder (looked for src/pages/ChatPage.tsx).\n"
        "Run this from the project root, or pass the frontend path as an argument."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore the Documents feature in the frontend.")
    parser.add_argument("frontend", nargs="?", help="path to the frontend folder (the one with src/)")
    parser.add_argument("--dry-run", action="store_true", help="check everything, change nothing")
    parser.add_argument("--force", action="store_true", help="overwrite DocumentPanel.tsx / useDocuments.ts even if not empty")
    args = parser.parse_args()

    root = find_frontend(args.frontend)
    src = root / "src"
    dry = args.dry_run

    print(f"Frontend: {root}" + ("  (dry run)" if dry else ""))

    try:
        write_new_file(src / "hooks" / "useDocuments.ts", USE_DOCUMENTS_TS, root, dry, args.force)
        write_new_file(src / "components" / "DocumentPanel.tsx", DOCUMENT_PANEL_TSX, root, dry, args.force)
        patch_types(src / "types.ts", root, dry)
        patch_api(src / "api.ts", root, dry)
        patch_context_panel(src / "components" / "ContextPanel.tsx", root, dry)
        patch_chat_page(src / "pages" / "ChatPage.tsx", root, dry)
        patch_css(src / "index.css", root, dry)
    except PatchError as error:
        print(f"\nERROR: {error}\nNothing after this point was changed.", file=sys.stderr)
        return 1

    print("\nDone." if not dry else "\nDry run finished, no files were changed.")
    print("Next:  cd", root)
    print("       npm install   (if node_modules is incomplete)")
    print("       npm test && npm run build")
    return 0


if __name__ == "__main__":
    sys.exit(main())