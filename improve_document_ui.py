#!/usr/bin/env python3
"""
improve_document_ui.py
======================

Moves document upload INTO the chat box and styles it.

Before: a raw, unstyled <details> block sat above the message box (the
        `doc-*` classes it uses had no CSS at all).
After:  - a paperclip button inside the chat box uploads a file directly
        - drag a file onto the chat box to upload it
        - attached / processing / uploading documents show as chips inside
          the chat box, above the text you type
        - a "Documents" button in the chat box opens a clean popover to
          tick (max 5), delete and review documents; Esc or a click outside closes it
        - upload errors show right in the chat box

Touches: components/DocumentPanel.tsx, components/ChatInput.tsx,
         pages/ChatPage.tsx, index.css

Usage:
    python improve_document_ui.py                    # auto-finds the frontend folder
    python improve_document_ui.py path/to/frontend
    python improve_document_ui.py --dry-run

Backups are saved once as <file>.bak-ui; re-running is safe (already-patched
files are skipped). Windows (CRLF) line endings are preserved.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

MARK_CSS = "/* ==== document ui v2 ==== */"
OLD_MARK_CSS = "/* ==== document feature ==== */"

# --------------------------------------------------------------------------
# DocumentPanel.tsx  (same props as before + optional onClose)
# --------------------------------------------------------------------------

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
  onClose,
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
  onClose?: () => void;
}) {
  const pickerRef = useRef<HTMLInputElement>(null);
  const limitReached = selectedIds.length >= MAX_SELECTED_DOCUMENTS;

  return (
    <section className="doc-panel" aria-label="Your documents">
      <header className="doc-panel-head">
        <div className="doc-panel-title">
          <strong>Documents</strong>
          <span className="doc-panel-count">{documents.length}</span>
          {selectedIds.length > 0 && (
            <span className="doc-selected-count">
              {selectedIds.length + " selected for questions"}
            </span>
          )}
        </div>

        {onClose && (
          <button
            type="button"
            className="doc-close"
            aria-label="Close documents"
            onClick={onClose}
          >
            ×
          </button>
        )}
      </header>

      <div className="doc-body">
        <div className="doc-toolbar">
          <input
            ref={pickerRef}
            id="document-file-input"
            className="sr-only"
            type="file"
            accept={ACCEPTED_EXTENSIONS.join(",")}
            tabIndex={-1}
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
            {ACCEPTED_EXTENSIONS.join(" ") + " · up to " + MAX_UPLOAD_MB + " MB"}
          </span>
        </div>

        {error && (
          <p className="doc-error" role="alert">
            <span>{error}</span>
            <button type="button" className="doc-link" onClick={onDismissError}>
              Dismiss
            </button>
          </p>
        )}

        {status === "error" && (
          <p className="doc-error" role="alert">
            <span>Could not load your documents.</span>
            <button type="button" className="doc-link" onClick={onReload}>
              Try again
            </button>
          </p>
        )}

        {status === "loading" && <p className="doc-hint">Loading…</p>}

        {status === "ready" && documents.length === 0 && (
          <p className="doc-empty">
            No documents yet. Upload one, then tick it to ask about it.
          </p>
        )}

        {documents.length > 0 && (
          <ul className="doc-list">
            {documents.map((document) => {
              const ready = document.status === "ready";
              const selected = selectedIds.includes(document.id);
              // A tick is only offered for a ready document; the 6th is refused
              const canTick = ready && (selected || !limitReached);

              return (
                <li
                  className={"doc-item" + (selected ? " is-selected" : "")}
                  key={document.id}
                >
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
        )}

        {limitReached && (
          <p className="doc-hint">
            {"You can ask about up to " + MAX_SELECTED_DOCUMENTS + " documents at once."}
          </p>
        )}
      </div>
    </section>
  );
}
'''

# --------------------------------------------------------------------------
# ChatInput.tsx  (adds two optional slots: header + tools)
# --------------------------------------------------------------------------

CHAT_INPUT_TSX = r'''import { useRef, type ReactNode } from "react";

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
        placeholder={sending ? "Type your next message while the answer is written…" : "Write a message…"}
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
            <span>Stop</span><span aria-hidden="true">■</span>
          </button>
        ) : (
          <button className="send-button" type="submit" disabled={sending || !value.trim()} aria-label="Send message">
            <span>Send</span><span aria-hidden="true">↑</span>
          </button>
        )}
      </div>
    </form>
  );
}
'''

# --------------------------------------------------------------------------
# ChatPage.tsx pieces
# --------------------------------------------------------------------------

CHATPAGE_STATE = r'''  const [draft, setDraft] = useState("");

  // ---- Documents in the chat box -------------------------------------
  const [docsOpen, setDocsOpen] = useState(false);
  const [dragging, setDragging] = useState(false);
  const composerRef = useRef<HTMLDivElement>(null);
  const attachInputRef = useRef<HTMLInputElement>(null);

  // Esc or a click outside the chat box closes the documents popover
  useEffect(() => {
    if (!docsOpen) return;

    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setDocsOpen(false);
    };

    const onPointer = (event: MouseEvent) => {
      if (
        composerRef.current &&
        !composerRef.current.contains(event.target as Node)
      ) {
        setDocsOpen(false);
      }
    };

    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onPointer);

    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onPointer);
    };
  }, [docsOpen]);

  const attachFiles = (files: FileList | null | undefined) => {
    const file = files?.[0];
    if (file) void uploadDocument(file);
  };
'''

CHATPAGE_ELEMENTS = r'''  // ---- Pieces of the chat box (attachments and buttons) ----------------
  const attachedDocs = selectedDocIds.flatMap((id) => {
    const found = documents.find((item) => item.id === id);
    return found ? [found] : [];
  });

  const busyDocs = documents.filter((item) => isBusy(item.status));
  const showDocError = Boolean(documentError) && !docsOpen;

  const attachmentBar =
    attachedDocs.length > 0 ||
    busyDocs.length > 0 ||
    uploading ||
    showDocError ? (
      <div className="doc-chips" aria-label="Attached documents">
        {uploading && (
          <span className="doc-chip is-busy">
            <span className="doc-spinner" aria-hidden="true" />
            <span className="doc-chip-name">Uploading…</span>
          </span>
        )}

        {busyDocs.map((item) => (
          <span className="doc-chip is-busy" key={item.id} title={item.filename}>
            <span className="doc-spinner" aria-hidden="true" />
            <span className="doc-chip-name">{item.filename}</span>
            <span className="doc-chip-state">{STATUS_LABELS[item.status]}</span>
          </span>
        ))}

        {attachedDocs.map((item) => (
          <span className="doc-chip" key={item.id} title={item.filename}>
            <span aria-hidden="true">📄</span>
            <span className="doc-chip-name">{item.filename}</span>
            <button
              type="button"
              aria-label={"Stop asking about " + item.filename}
              onClick={() => toggleDocument(item.id)}
            >
              ×
            </button>
          </span>
        ))}

        {showDocError && (
          <span className="doc-chip is-error" role="alert">
            <span className="doc-chip-name">{documentError}</span>
            <button
              type="button"
              aria-label="Dismiss"
              onClick={dismissDocumentError}
            >
              ×
            </button>
          </span>
        )}
      </div>
    ) : null;

  const composerTools = (
    <>
      <input
        ref={attachInputRef}
        id="composer-file-input"
        className="sr-only"
        type="file"
        accept={ACCEPTED_EXTENSIONS.join(",")}
        tabIndex={-1}
        onChange={(event) => {
          const files = event.target.files;
          attachFiles(files);
          event.target.value = "";
        }}
      />

      <button
        type="button"
        className="composer-tool"
        disabled={uploading}
        aria-label="Attach a document"
        title="Attach a document (PDF, DOCX, TXT, MD)"
        onClick={() => attachInputRef.current?.click()}
      >
        <svg
          viewBox="0 0 24 24"
          width="16"
          height="16"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="m21.4 11.1-8.5 8.5a5.5 5.5 0 0 1-7.8-7.8l8.5-8.5a3.7 3.7 0 0 1 5.2 5.2l-8.5 8.5a1.8 1.8 0 0 1-2.6-2.6l7.8-7.8" />
        </svg>
      </button>

      <button
        type="button"
        className={"composer-tool composer-tool--text" + (docsOpen ? " is-active" : "")}
        aria-expanded={docsOpen}
        onClick={() => setDocsOpen((open) => !open)}
      >
        Documents
        {selectedDocIds.length > 0 && (
          <span className="composer-badge">{selectedDocIds.length}</span>
        )}
      </button>
    </>
  );

  const handleLogout = () => {
'''

CHATPAGE_FOOTER_OLD = '''          <DocumentPanel
            documents={documents}
            status={documentStatus}
            selectedIds={selectedDocIds}
            uploading={uploading}
            error={documentError}
            onToggle={toggleDocument}
            onUpload={(file) => void uploadDocument(file)}
            onRemove={(id) => void removeDocument(id)}
            onDismissError={dismissDocumentError}
            onReload={() => void reloadDocuments()}
          />

          <ChatInput
            value={draft}
            onChange={setDraft}
            onSend={() => handleSend()}
            onStop={() => cancel(selectedId)}
            sending={isSending}
          />
'''

CHATPAGE_FOOTER_NEW = '''          <div
            ref={composerRef}
            className={"composer-wrap" + (dragging ? " is-dragging" : "")}
            onDragOver={(event) => {
              if (event.dataTransfer.types.includes("Files")) {
                event.preventDefault();
                setDragging(true);
              }
            }}
            onDragLeave={(event) => {
              if (!event.currentTarget.contains(event.relatedTarget as Node)) {
                setDragging(false);
              }
            }}
            onDrop={(event) => {
              event.preventDefault();
              setDragging(false);
              attachFiles(event.dataTransfer.files);
            }}
          >
            {docsOpen && (
              <div className="doc-popover">
                <DocumentPanel
                  documents={documents}
                  status={documentStatus}
                  selectedIds={selectedDocIds}
                  uploading={uploading}
                  error={documentError}
                  onToggle={toggleDocument}
                  onUpload={(file) => void uploadDocument(file)}
                  onRemove={(id) => void removeDocument(id)}
                  onDismissError={dismissDocumentError}
                  onReload={() => void reloadDocuments()}
                  onClose={() => setDocsOpen(false)}
                />
              </div>
            )}

            {dragging && (
              <div className="doc-drop" aria-hidden="true">
                Drop a file to upload it
              </div>
            )}

            <ChatInput
              value={draft}
              onChange={setDraft}
              onSend={() => handleSend()}
              onStop={() => cancel(selectedId)}
              sending={isSending}
              header={attachmentBar}
              tools={composerTools}
            />
          </div>
'''

# --------------------------------------------------------------------------
# CSS
# --------------------------------------------------------------------------

CSS_V2 = r'''
/* ==== document ui v2 ==== */
.composer-wrap { position: relative; width: min(820px, 100%); margin: 0 auto; }
.composer-wrap .chat-input-form { width: 100%; }
.composer-wrap.is-dragging .chat-input-form { border-color: var(--accent); }
.doc-drop { position: absolute; z-index: 7; inset: 0; display: grid; place-items: center; border: 2px dashed var(--accent); border-radius: 14px; background: #f4f2ffec; color: var(--accent-dark); font-size: 12px; font-weight: 700; pointer-events: none; }

/* Buttons at the bottom-left of the chat box */
.composer-tools { display: flex; min-width: 0; align-items: center; gap: 4px; }
.composer-tools > span { margin-left: 6px; overflow: hidden; color: #aaa9b7; font-size: 9px; text-overflow: ellipsis; white-space: nowrap; }
.composer-tool { display: inline-flex; height: 30px; min-width: 30px; align-items: center; justify-content: center; gap: 6px; padding: 0 7px; border: 0; border-radius: 8px; background: transparent; color: #77758c; cursor: pointer; font-size: 10px; font-weight: 700; transition: background .15s, color .15s; }
.composer-tool:hover:not(:disabled), .composer-tool.is-active { background: var(--accent-soft); color: var(--accent-dark); }
.composer-tool--text { padding: 0 10px; }
.composer-badge { display: inline-grid; min-width: 16px; height: 16px; place-items: center; padding: 0 4px; border-radius: 8px; background: var(--accent); color: #fff; font-size: 9px; font-weight: 800; }

/* Attached documents, inside the chat box above the text */
.doc-chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 8px; }
.doc-chip { display: inline-flex; max-width: 100%; min-width: 0; align-items: center; gap: 6px; padding: 4px 6px 4px 9px; border: 1px solid #d8d3fb; border-radius: 9px; background: #f7f6ff; color: var(--accent-dark); font-size: 10px; font-weight: 600; }
.doc-chip-name { max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.doc-chip-state { color: #8a6a27; font-weight: 700; }
.doc-chip button { display: grid; width: 16px; height: 16px; place-items: center; padding: 0; border: 0; border-radius: 50%; background: transparent; color: inherit; cursor: pointer; font-size: 13px; line-height: 1; }
.doc-chip button:hover { background: #e4e0fb; }
.doc-chip.is-busy { border-color: #efe2bb; background: #fffaf0; color: #7a5c1b; }
.doc-chip.is-error { border-color: #eed0d0; background: #fff7f7; color: #a14545; }
.doc-chip.is-error .doc-chip-name { max-width: 340px; white-space: normal; }
.doc-spinner { width: 10px; height: 10px; flex: 0 0 10px; border: 2px solid currentColor; border-right-color: transparent; border-radius: 50%; opacity: .7; animation: doc-spin .8s linear infinite; }
@keyframes doc-spin { to { transform: rotate(360deg); } }

/* The documents popover, opened above the chat box */
.doc-popover { position: absolute; z-index: 6; right: 0; bottom: calc(100% + 10px); left: 0; }
.doc-panel { display: flex; max-height: min(400px, 52dvh); flex-direction: column; overflow: hidden; border: 1px solid #e3e2eb; border-radius: 14px; background: #fff; box-shadow: 0 16px 44px #29254b22; }
.doc-panel-head { display: flex; flex: 0 0 auto; align-items: center; justify-content: space-between; gap: 10px; padding: 11px 12px 11px 16px; border-bottom: 1px solid #f0eff4; }
.doc-panel-title { display: flex; min-width: 0; align-items: center; gap: 8px; font-size: 12px; }
.doc-panel-count { padding: 1px 7px; border-radius: 9px; background: #efeff5; color: #6d6c80; font-size: 9px; font-weight: 700; }
.doc-selected-count { color: var(--accent-dark); font-size: 10px; font-weight: 600; }
.doc-close { display: grid; width: 26px; height: 26px; place-items: center; border: 0; border-radius: 7px; background: transparent; color: #77758c; cursor: pointer; font-size: 18px; line-height: 1; }
.doc-close:hover { background: #f0eff6; }
.doc-body { display: grid; align-content: start; gap: 10px; overflow-y: auto; padding: 12px 16px 16px; }
.doc-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.doc-upload-button { padding: 8px 12px; border: 1px dashed #b9b3ef; border-radius: 9px; background: #faf9ff; color: var(--accent-dark); cursor: pointer; font-size: 11px; font-weight: 700; transition: background .15s; }
.doc-upload-button:hover:not(:disabled) { background: var(--accent-soft); }
.doc-hint { margin: 0; color: var(--muted); font-size: 10px; line-height: 1.5; }
.doc-empty { margin: 0; padding: 18px 12px; border: 1px dashed #e3e2eb; border-radius: 10px; color: var(--muted); font-size: 11px; line-height: 1.5; text-align: center; }
.doc-error { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin: 0; padding: 8px 10px; border: 1px solid #eed0d0; border-radius: 9px; background: #fff7f7; color: #a14545; font-size: 10px; line-height: 1.5; }
.doc-link { padding: 0; border: 0; background: none; color: var(--accent-dark); cursor: pointer; font-size: inherit; font-weight: 700; text-decoration: underline; white-space: nowrap; }
.doc-list { display: grid; gap: 6px; margin: 0; padding: 0; list-style: none; }
.doc-item { display: flex; align-items: center; gap: 10px; padding: 9px 10px; border: 1px solid #ecebf3; border-radius: 10px; background: #fff; transition: border-color .15s, background .15s; }
.doc-item:hover { background: #fbfbfd; }
.doc-item.is-selected { border-color: #c9c3f4; background: #faf9ff; }
.doc-check { display: grid; place-items: center; cursor: pointer; }
.doc-check input { width: 15px; height: 15px; margin: 0; accent-color: var(--accent); cursor: pointer; }
.doc-check input:disabled { cursor: not-allowed; }
.doc-info { display: flex; min-width: 0; flex: 1; flex-wrap: wrap; align-items: center; gap: 4px 8px; }
.doc-name { max-width: 100%; overflow: hidden; color: #2f2e43; font-size: 11px; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
.doc-badge { padding: 2px 7px; border-radius: 10px; background: #efeff5; color: #6d6c80; font-size: 9px; font-weight: 700; }
.doc-badge.is-ready { background: #e6f5ee; color: var(--green); }
.doc-badge.is-processing, .doc-badge.is-deleting { background: #fbf3df; color: #8a6a27; }
.doc-badge.is-failed { background: #fbe9e9; color: var(--red); }
.doc-failure { flex-basis: 100%; color: #a14545; font-size: 10px; line-height: 1.5; }
.doc-delete { display: grid; width: 24px; height: 24px; flex: 0 0 24px; place-items: center; border: 0; border-radius: 7px; background: transparent; color: #9a99aa; cursor: pointer; font-size: 16px; line-height: 1; }
.doc-delete:hover:not(:disabled) { background: #fbe9e9; color: var(--red); }

.context-item--document { border-color: #dcd8fa; background: #faf9ff; }

@media (max-width: 560px) {
  .composer-tools > span { display: none; }
  .doc-chip-name { max-width: 130px; }
  .doc-panel { max-height: 60dvh; }
}
'''

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


class PatchError(Exception):
    pass


def read_text(path: Path) -> tuple[str, str]:
    raw = path.read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n"), newline


def write_text(path: Path, text: str, newline: str, dry: bool) -> None:
    if not dry:
        path.write_bytes(text.replace("\n", newline).encode("utf-8"))


def backup(path: Path, dry: bool) -> None:
    target = path.with_name(path.name + ".bak-ui")
    if path.exists() and not target.exists() and not dry:
        shutil.copy2(path, target)


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise PatchError(
            f"{label}: expected the anchor text exactly once, found {count}. "
            "Your file differs from the version this script was written for."
        )
    return text.replace(old, new)


def log(action: str, path: Path, root: Path) -> None:
    print(f"  {action:<9} {path.relative_to(root)}")


def rewrite(path: Path, content: str, marker: str, root: Path, dry: bool) -> None:
    text, nl = read_text(path)
    if marker in text:
        return log("ok", path, root)
    backup(path, dry)
    write_text(path, content, nl, dry)
    log("rewritten", path, root)


# --------------------------------------------------------------------------
# patches
# --------------------------------------------------------------------------


def patch_chat_page(path: Path, root: Path, dry: bool) -> None:
    text, nl = read_text(path)
    if "composer-wrap" in text:
        return log("ok", path, root)

    text = replace_once(
        text,
        'import { DocumentPanel } from "../components/DocumentPanel";\n',
        'import { DocumentPanel } from "../components/DocumentPanel";\n'
        'import { STATUS_LABELS, isBusy } from "../components/documentStatus";\n'
        'import { ACCEPTED_EXTENSIONS } from "../services/documentLimits";\n',
        "ChatPage imports",
    )
    text = replace_once(
        text,
        '  const [draft, setDraft] = useState("");\n',
        CHATPAGE_STATE,
        "ChatPage state",
    )
    text = replace_once(
        text,
        "  const handleLogout = () => {\n",
        CHATPAGE_ELEMENTS,
        "ChatPage elements",
    )
    text = replace_once(
        text,
        CHATPAGE_FOOTER_OLD,
        CHATPAGE_FOOTER_NEW,
        "ChatPage footer",
    )

    backup(path, dry)
    write_text(path, text, nl, dry)
    log("patched", path, root)


def patch_css(path: Path, root: Path, dry: bool) -> None:
    text, nl = read_text(path)
    if MARK_CSS in text:
        return log("ok", path, root)

    # Remove the dead CSS from the first script (it styled classes that are
    # no longer used), but only if nothing else was added after it.
    start = text.find(OLD_MARK_CSS)
    if start != -1:
        tail = text[start:]
        allowed = re.compile(r"^(\.documents?-|\.documents-|\.context-item--document|@media|  \.|\}|/\*|$)")
        if all(allowed.match(line) for line in tail.split("\n")):
            text = text[:start].rstrip("\n") + "\n"

    text = text.rstrip("\n") + "\n" + CSS_V2
    backup(path, dry)
    write_text(path, text, nl, dry)
    log("patched", path, root)


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
    parser = argparse.ArgumentParser(description="Put document upload in the chat box and style it.")
    parser.add_argument("frontend", nargs="?", help="path to the frontend folder (the one with src/)")
    parser.add_argument("--dry-run", action="store_true", help="check everything, change nothing")
    args = parser.parse_args()

    root = find_frontend(args.frontend)
    src = root / "src"
    dry = args.dry_run
    print(f"Frontend: {root}" + ("  (dry run)" if dry else ""))

    try:
        # Check every anchor before writing anything, so a mismatch changes nothing
        probe, _ = read_text(src / "pages" / "ChatPage.tsx")
        if "composer-wrap" not in probe:
            for label, anchor in (
                ("DocumentPanel import", 'import { DocumentPanel } from "../components/DocumentPanel";\n'),
                ("draft state", '  const [draft, setDraft] = useState("");\n'),
                ("handleLogout", "  const handleLogout = () => {\n"),
                ("footer", CHATPAGE_FOOTER_OLD),
            ):
                if probe.count(anchor) != 1:
                    raise PatchError(
                        f"ChatPage.tsx ({label}) does not look the way this script expects. "
                        "Nothing was changed."
                    )

        rewrite(src / "components" / "DocumentPanel.tsx", DOCUMENT_PANEL_TSX, "doc-panel-head", root, dry)
        rewrite(src / "components" / "ChatInput.tsx", CHAT_INPUT_TSX, "composer-tools", root, dry)
        patch_chat_page(src / "pages" / "ChatPage.tsx", root, dry)
        patch_css(src / "index.css", root, dry)
    except PatchError as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        return 1

    print("\nDone." if not dry else "\nDry run finished, no files were changed.")
    print("Next:  cd", root)
    print("       npm test && npm run dev")
    return 0


if __name__ == "__main__":
    sys.exit(main())