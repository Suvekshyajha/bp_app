import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { POLL_INTERVAL_MS, useDocuments } from "./useDocuments";
import {
  NotFoundError,
  UnauthorizedError,
  deleteDocument,
  getDocuments,
  uploadDocument,
} from "../api";
import type { DocumentInfo, DocumentStatus } from "../types";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  getDocuments: vi.fn(),
  uploadDocument: vi.fn(),
  deleteDocument: vi.fn(),
}));

const getDocumentsMock = vi.mocked(getDocuments);
const uploadMock = vi.mocked(uploadDocument);
const deleteMock = vi.mocked(deleteDocument);

function doc(id: string, status: DocumentStatus = "ready", day = 1): DocumentInfo {
  return {
    id,
    filename: id + ".pdf",
    status,
    createdAt: `2026-11-0${day}T10:00:00+00:00`,
  };
}

const ids = (list: { id: string }[]) => list.map((d) => d.id);

// Lets the first load finish while fake timers are on
async function settle() {
  await act(async () => {
    await Promise.resolve();
  });
}

beforeEach(() => {
  getDocumentsMock.mockReset();
  uploadMock.mockReset();
  deleteMock.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("loading", () => {
  it("loads the list, newest first", async () => {
    getDocumentsMock.mockResolvedValue([doc("old", "ready", 1), doc("new", "ready", 3)]);

    const { result } = renderHook(() => useDocuments());
    await waitFor(() => expect(result.current.status).toBe("ready"));

    expect(ids(result.current.documents)).toEqual(["new", "old"]);
  });

  it("reports an error when the first load fails", async () => {
    getDocumentsMock.mockRejectedValue(new Error("down"));

    const { result } = renderHook(() => useDocuments());
    await waitFor(() => expect(result.current.status).toBe("error"));
  });

  it("logs out on a 401", async () => {
    getDocumentsMock.mockRejectedValue(new UnauthorizedError());
    const onAuthError = vi.fn();

    renderHook(() => useDocuments(onAuthError));
    await waitFor(() => expect(onAuthError).toHaveBeenCalledTimes(1));
  });
});

describe("polling", () => {
  it("does not poll when every document is settled", async () => {
    vi.useFakeTimers();
    getDocumentsMock.mockResolvedValue([doc("a", "ready"), doc("b", "failed")]);

    renderHook(() => useDocuments());
    await settle();
    expect(getDocumentsMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 5);
    });

    expect(getDocumentsMock).toHaveBeenCalledTimes(1);
  });

  it("polls every 2 s while a document is pending or processing, then stops", async () => {
    vi.useFakeTimers();
    getDocumentsMock
      .mockResolvedValueOnce([doc("a", "pending")])
      .mockResolvedValueOnce([doc("a", "processing")])
      .mockResolvedValue([doc("a", "ready")]);

    const { result } = renderHook(() => useDocuments());
    await settle();
    expect(result.current.documents[0].status).toBe("pending");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    });
    expect(result.current.documents[0].status).toBe("processing");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    });
    expect(result.current.documents[0].status).toBe("ready");
    expect(getDocumentsMock).toHaveBeenCalledTimes(3);

    // Done: no more calls
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 5);
    });
    expect(getDocumentsMock).toHaveBeenCalledTimes(3);
  });

  it("stops polling when the page is closed", async () => {
    vi.useFakeTimers();
    getDocumentsMock.mockResolvedValue([doc("a", "processing")]);

    const { unmount } = renderHook(() => useDocuments());
    await settle();
    unmount();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 5);
    });
    expect(getDocumentsMock).toHaveBeenCalledTimes(1);
  });

  it("keeps the list when a refresh fails", async () => {
    vi.useFakeTimers();
    getDocumentsMock
      .mockResolvedValueOnce([doc("a", "processing")])
      .mockRejectedValue(new Error("blip"));

    const { result } = renderHook(() => useDocuments());
    await settle();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    });

    expect(result.current.status).toBe("ready");
    expect(ids(result.current.documents)).toEqual(["a"]);
  });
});

describe("upload", () => {
  it("adds the new document, which starts the polling", async () => {
    getDocumentsMock.mockResolvedValue([]);
    uploadMock.mockResolvedValue(doc("n", "pending", 2));

    const { result } = renderHook(() => useDocuments());
    await waitFor(() => expect(result.current.status).toBe("ready"));

    await act(async () => {
      await result.current.upload(new File(["hi"], "n.txt"));
    });

    expect(ids(result.current.documents)).toEqual(["n"]);
    expect(result.current.uploading).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it("the same file twice leaves one entry", async () => {
    getDocumentsMock.mockResolvedValue([doc("n", "ready", 2)]);
    uploadMock.mockResolvedValue(doc("n", "ready", 2));

    const { result } = renderHook(() => useDocuments());
    await waitFor(() => expect(result.current.status).toBe("ready"));

    await act(async () => {
      await result.current.upload(new File(["hi"], "n.txt"));
    });

    expect(ids(result.current.documents)).toEqual(["n"]);
  });

  it("refuses a wrong type or an empty file without calling the server", async () => {
    getDocumentsMock.mockResolvedValue([]);

    const { result } = renderHook(() => useDocuments());
    await waitFor(() => expect(result.current.status).toBe("ready"));

    await act(async () => {
      await result.current.upload(new File(["MZ"], "setup.exe"));
    });
    expect(result.current.error).toMatch(/only .pdf/i);

    await act(async () => {
      await result.current.upload(new File([], "empty.txt"));
    });
    expect(result.current.error).toMatch(/empty/i);

    expect(uploadMock).not.toHaveBeenCalled();
  });

  it("shows the server's reason when the upload is refused", async () => {
    getDocumentsMock.mockResolvedValue([]);
    uploadMock.mockRejectedValue(new Error("You are uploading too quickly."));

    const { result } = renderHook(() => useDocuments());
    await waitFor(() => expect(result.current.status).toBe("ready"));

    await act(async () => {
      await result.current.upload(new File(["hi"], "n.txt"));
    });

    expect(result.current.error).toBe("You are uploading too quickly.");
    expect(result.current.uploading).toBe(false);
  });

  it("logs out on a 401 instead of showing an error", async () => {
    getDocumentsMock.mockResolvedValue([]);
    uploadMock.mockRejectedValue(new UnauthorizedError());
    const onAuthError = vi.fn();

    const { result } = renderHook(() => useDocuments(onAuthError));
    await waitFor(() => expect(result.current.status).toBe("ready"));

    await act(async () => {
      await result.current.upload(new File(["hi"], "n.txt"));
    });

    expect(onAuthError).toHaveBeenCalled();
    expect(result.current.error).toBeNull();
  });
});

describe("remove", () => {
  it("removes the document after the server confirms", async () => {
    getDocumentsMock.mockResolvedValue([doc("a"), doc("b")]);
    deleteMock.mockResolvedValue(undefined);

    const { result } = renderHook(() => useDocuments());
    await waitFor(() => expect(result.current.status).toBe("ready"));

    await act(async () => {
      await result.current.remove("a");
    });

    expect(deleteMock).toHaveBeenCalledWith("a");
    expect(ids(result.current.documents)).toEqual(["b"]);
  });

  it("keeps it and shows an error when the delete fails", async () => {
    getDocumentsMock.mockResolvedValue([doc("a")]);
    deleteMock.mockRejectedValue(new Error("The document service is unavailable."));

    const { result } = renderHook(() => useDocuments());
    await waitFor(() => expect(result.current.status).toBe("ready"));

    await act(async () => {
      await result.current.remove("a");
    });

    expect(ids(result.current.documents)).toEqual(["a"]);
    expect(result.current.error).toMatch(/unavailable/);
  });

  it("treats 'not found' as already deleted", async () => {
    getDocumentsMock.mockResolvedValue([doc("a")]);
    deleteMock.mockRejectedValue(new NotFoundError());

    const { result } = renderHook(() => useDocuments());
    await waitFor(() => expect(result.current.status).toBe("ready"));

    await act(async () => {
      await result.current.remove("a");
    });

    expect(result.current.documents).toEqual([]);
    expect(result.current.error).toBeNull();
  });
});
