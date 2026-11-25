import { useCallback, useEffect, useRef, useState } from "react";
import type { DocumentInfo } from "../types";
import {
  NotFoundError,
  UnauthorizedError,
  deleteDocument,
  getDocuments,
  uploadDocument,
} from "../api";
import { describeDocumentError, isBusy } from "../components/documentStatus";
import { checkFile } from "../services/documentLimits";

export type DocumentListStatus = "loading" | "ready" | "error";

// How often the list is refreshed while something is still being processed
export const POLL_INTERVAL_MS = 2000;

// A short message shown to the user once something finished (or failed later)
export interface Notice {
  id: number;
  kind: "success" | "info" | "error";
  message: string;
}

// An upload the server accepted but has not finished processing yet
interface WatchedUpload {
  filename: string;
  // True once a refresh has shown it in the list. Only then can "missing"
  // mean it was deleted, rather than a refresh that started before the upload.
  seen: boolean;
}

// New documents on top
function newestFirst(list: DocumentInfo[]): DocumentInfo[] {
  return [...list].sort((a, b) => b.createdAt.localeCompare(a.createdAt));
}

export function useDocuments(onAuthError?: () => void) {
  const [documents, setDocuments] = useState<DocumentInfo[]>([]);
  const [status, setStatus] = useState<DocumentListStatus>("loading");
  // Names of the files being sent right now (one entry per upload)
  const [uploadingNames, setUploadingNames] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notices, setNotices] = useState<Notice[]>([]);

  const onAuthErrorRef = useRef(onAuthError);
  const refreshing = useRef(false);
  const watched = useRef(new Map<string, WatchedUpload>());
  const noticeCount = useRef(0);
  // Bumped when an upload changes the list, so a refresh that started earlier
  // (and may not know the new document) cannot overwrite it
  const generation = useRef(0);

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

  const addNotice = useCallback((kind: Notice["kind"], message: string) => {
    noticeCount.current += 1;
    const notice: Notice = { id: noticeCount.current, kind, message };
    setNotices((current) => [...current, notice]);
  }, []);

  // An upload is only "done" when the document is ready to be searched.
  // Called with every fresh list: reports the uploads that just finished.
  const settleWatched = useCallback(
    (list: DocumentInfo[]) => {
      for (const [id, upload] of [...watched.current]) {
        const item = list.find((d) => d.id === id);

        if (!item) {
          // Gone after we saw it: deleted, nothing to report
          if (upload.seen) watched.current.delete(id);
          continue;
        }

        upload.seen = true;

        if (item.status === "ready") {
          watched.current.delete(id);
          addNotice(
            "success",
            `"${upload.filename}" was uploaded and is ready for questions.`,
          );
        } else if (item.status === "failed") {
          watched.current.delete(id);
          addNotice(
            "error",
            `"${upload.filename}" could not be processed. ` +
              describeDocumentError(item.error),
          );
        } else if (item.status === "deleting") {
          watched.current.delete(id);
        }
      }
    },
    [addNotice],
  );

  // Loads the list. A refresh that is still running is not started twice.
  const refresh = useCallback(async (): Promise<void> => {
    if (refreshing.current) return;
    refreshing.current = true;

    try {
      for (;;) {
        const started = generation.current;
        let list: DocumentInfo[];

        try {
          list = await getDocuments();
        } catch (failure) {
          handleFailure(failure);
          // A failed refresh keeps the list we already have
          setStatus((current) => (current === "ready" ? current : "error"));
          return;
        }

        // An upload changed the list while we were asking, so this answer may
        // not know the new document: ask again instead of showing it
        if (started !== generation.current) continue;

        setDocuments(newestFirst(list));
        setStatus("ready");
        settleWatched(list);
        return;
      }
    } finally {
      refreshing.current = false;
    }
  }, [handleFailure, settleWatched]);

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
        setError(`Could not upload "${file.name}": ${problem}`);
        return;
      }

      setError(null);
      setUploadingNames((current) => [...current, file.name]);

      try {
        const created = await uploadDocument(file);

        generation.current += 1;

        // The same file twice gives back the same id: replace, never duplicate
        setDocuments((current) =>
          newestFirst([created, ...current.filter((d) => d.id !== created.id)]),
        );

        // The file arrived, but "uploaded" is not "ready": the server still
        // has to read and index it. Say so only when that has really happened.
        if (created.status === "ready") {
          addNotice("info", `"${created.filename}" is already uploaded and ready.`);
        } else if (created.status === "failed") {
          addNotice(
            "error",
            `"${created.filename}" could not be processed. ` +
              describeDocumentError(created.error),
          );
        } else {
          watched.current.set(created.id, {
            filename: created.filename,
            seen: false,
          });
        }
      } catch (failure) {
        if (!handleFailure(failure)) {
          const reason =
            failure instanceof Error
              ? failure.message
              : "Please try again.";
          setError(`Could not upload "${file.name}": ${reason}`);
        }
      } finally {
        setUploadingNames((current) => {
          const index = current.indexOf(file.name);
          return index < 0 ? current : current.filter((_, i) => i !== index);
        });
      }
    },
    [handleFailure, addNotice],
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

      watched.current.delete(id);
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
    uploading: uploadingNames.length > 0,
    uploadingNames,
    error,
    notices,
    upload,
    remove,
    refresh,
    dismissError,
    dismissNotice,
  };
}