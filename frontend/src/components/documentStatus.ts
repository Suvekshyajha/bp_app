
export const STATUS_LABELS: Record<string, string> = {
  ready: "Ready",
  pending: "Queued",
  processing: "Processing",
  completed: "Ready",
  failed: "Failed",
  uploaded: "Queued",
};

export function describeDocumentError(error: unknown): string {
  const code =
    error instanceof Error
      ? error.message
      : typeof error === "string"
        ? error
        : "";

  const messages: Record<string, string> = {
    no_text: "No readable text found in this document.",
    unsupported_type: "This document type is not supported.",
    file_too_large: "The document is too large.",
    processing_failed: "The document could not be processed.",
  };

  return messages[code.toLowerCase()] ??
    "This document could not be processed.";
}

export function isBusy(status: string): boolean {
  return ["pending", "processing", "uploaded"].includes(
    status.toLowerCase()
  );
}
