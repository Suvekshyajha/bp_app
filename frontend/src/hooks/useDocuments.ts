import { useCallback, useEffect, useRef, useState } from "react";
import type { DocumentInfo } from "../types";
import {
  NotFoundError,
  UnauthorizedError,
  deleteDocument,
  getDocuments,
  uploadDocument,
} from "../api";
import { isBusy } from "../components/documentStatus";
import { checkFile } from "../services/documentLimits";

export type DocumentListStatus = "loading" | "ready" | "error";

// How often the list is refreshed while something is still being processed
export const POLL_INTERVAL_MS = 2000;

// New documents on top
function newestFirst(list: DocumentInfo[]): DocumentInfo[] {
  return [...list].sort((a, b) => b.createdAt.localeCompare(a.createdAt));
}

export function useDocuments(onAuthError?: () => void) {
  const [documents, setDocuments] = useState<DocumentInfo[]>([]);
  const [status, setStatus] = useState<DocumentListStatus>("loading");
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onAuthErrorRef = useRef(onAuthError);
  const refreshing = useRef(false);

  useEffect(() => {
    onAuthErrorRef.current = onAuthError;
  }, [onAuthError]);

  // Every call may find out the session ended
  const handleFailure = useCallback((failure: unknown): boolean => {
    if (failure instanceof UnauthorizedError) {
      onAuthErrorRef.current?.();
      return true;
    }
    return false;
  }, []);

  // Loads the list. A refresh that is still running is not started twice.
  const refresh = useCallback((): Promise<void> => {
    if (refreshing.current) return Promise.resolve();
    refreshing.current = true;

    return getDocuments()
      .then((list) => {
        setDocuments(newestFirst(list));
        setStatus("ready");
      })
      .catch((failure: unknown) => {
        handleFailure(failure);
        // A failed refresh keeps the list we already have
        setStatus((current) => (current === "ready" ? current : "error"));
      })
      .finally(() => {
        refreshing.current = false;
      });
  }, [handleFailure]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Poll only while something is pending or processing, then stop
  const shouldPoll = documents.some((document) => isBusy(document.status));

  useEffect(() => {
    if (!shouldPoll) return;

    const timer = window.setInterval(() => void refresh(), POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [shouldPoll, refresh]);

  const upload = useCallback(
    async (file: File) => {
      const problem = checkFile(file);

      if (problem) {
        setError(problem);
        return;
      }

      setError(null);
      setUploading(true);

      try {
        const created = await uploadDocument(file);

        // The same file twice gives back the same id: replace, never duplicate
        setDocuments((current) =>
          newestFirst([created, ...current.filter((d) => d.id !== created.id)]),
        );
      } catch (failure) {
        if (!handleFailure(failure)) {
          setError(
            failure instanceof Error
              ? failure.message
              : "The upload failed. Please try again.",
          );
        }
      } finally {
        setUploading(false);
      }
    },
    [handleFailure],
  );

  const remove = useCallback(
    async (id: string) => {
      setError(null);

      try {
        await deleteDocument(id);
      } catch (failure) {
        // "Not found" means it is already gone: that is what we wanted
        if (!(failure instanceof NotFoundError)) {
          if (!handleFailure(failure)) {
            setError(
              failure instanceof Error
                ? failure.message
                : "The document could not be deleted.",
            );
          }
          return;
        }
      }

      setDocuments((current) => current.filter((d) => d.id !== id));
    },
    [handleFailure],
  );

  const dismissError = useCallback(() => setError(null), []);

  return {
    documents,
    status,
    uploading,
    error,
    upload,
    remove,
    refresh,
    dismissError,
  };
}
