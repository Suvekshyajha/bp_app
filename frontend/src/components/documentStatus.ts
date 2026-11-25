import type { DocumentStatus } from "../types";

export const STATUS_LABELS: Record<DocumentStatus, string> = {
  pending: "Queued",
  processing: "Processing",
  ready: "Ready",
  failed: "Failed",
  deleting: "Deleting",
};

// The ingestion service sends a short safe code. Say what it means.
const ERROR_TEXT: Record<string, string> = {
  unsupported_or_corrupt:
    "This file could not be read. It may be damaged or not the type its name says.",
  too_large: "This file is too large.",
  no_text:
    "No readable text was found. Scanned documents (images) are not supported.",
  timeout: "Reading this file took too long. Try a smaller file.",
  too_many_chunks: "This file is too long to index. Try a shorter one.",
};

export function describeDocumentError(code?: string): string {
  return (code && ERROR_TEXT[code]) || "This document could not be processed.";
}

// Documents the server is still working on: the list keeps refreshing for these
export function isBusy(status: DocumentStatus): boolean {
  return status === "pending" || status === "processing";
}
