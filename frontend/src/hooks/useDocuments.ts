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

// A short message shown as a toast (see components/toasts.tsx)
export type Notice = {
  id: number;
  kind: "success" | "info" | "error";
  message: string;
};

// Never keep more than this many toasts on screen
const MAX_NOTICES = 4;

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
  const [notices, setNotices] = useState<Notice[]>([]);

  // The status each document had the last time we saw it. A toast is shown
  // when a document we saw BUSY is now ready or failed, never for one that
  // was already finished when the list first loaded.
  const lastStatus = useRef(new Map<string, string>());
  const nextNoticeId = useRef(1);

  const onAuthErrorRef = useRef(onAuthError);
  const refreshing = useRef(false);

  useEffect(() => {
    onAuthErrorRef.current = onAuthError;
  }, [onAuthError]);

  const notify = useCallback((kind: Notice["kind"], message: string) => {
    const id = nextNoticeId.current++;
    setNotices((current) => [...current, { id, kind, message }].slice(-MAX_NOTICES));
  }, []);

  // Compares a fresh list with what we saw before and announces the changes
  const announceFinished = useCallback(
    (list: DocumentInfo[]) => {
      for (const document of list) {
        const before = lastStatus.current.get(document.id);

        if (before && isBusy(before)) {
          if (document.status === "ready") {
            notify(
              "success",
              `"${document.filename}" was uploaded and is ready for questions.`,
            );
          } else if (document.status === "failed") {
            notify("error", `"${document.filename}" could not be processed.`);
          }
        }

        lastStatus.current.set(document.id, document.status);
      }
    },
    [notify],
  );

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
        announceFinished(list);
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
  }, [handleFailure, announceFinished]);

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
        lastStatus.current.set(created.id, created.status);

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

      lastStatus.current.delete(id);
      setDocuments((current) => current.filter((d) => d.id !== id));
    },
    [handleFailure],
  );

  const dismissError = useCallback(() => setError(null), []);

  const dismissNotice = useCallback(
    (id: number) => setNotices((current) => current.filter((n) => n.id !== id)),
    [],
  );

  return {
    documents,
    status,
    uploading,
    error,
    notices,
    upload,
    remove,
    refresh,
    dismissError,
    dismissNotice,
  };
}