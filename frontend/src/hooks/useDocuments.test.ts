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

    expect(result.current.error).toBe(
      'Could not upload "n.txt": You are uploading too quickly.',
    );
    expect(result.current.uploading).toBe(false);
    expect(result.current.notices).toEqual([]);
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

describe("upload feedback", () => {
  async function ready() {
    const hook = renderHook(() => useDocuments());
    await act(async () => {
      await Promise.resolve();
    });
    return hook;
  }

  it("does not claim success when the file has only arrived", async () => {
    vi.useFakeTimers();
    getDocumentsMock.mockResolvedValue([]);
    uploadMock.mockResolvedValue(doc("n", "pending", 2));

    const { result } = await ready();
    await act(async () => {
      await result.current.upload(new File(["hi"], "n.txt"));
    });

    expect(result.current.notices).toEqual([]);
    expect(result.current.uploading).toBe(false);
    expect(result.current.documents[0].status).toBe("pending");
  });

  it("reports success only once the document is ready", async () => {
    vi.useFakeTimers();
    getDocumentsMock
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([doc("n", "processing", 2)])
      .mockResolvedValue([doc("n", "ready", 2)]);
    uploadMock.mockResolvedValue(doc("n", "pending", 2));

    const { result } = await ready();
    await act(async () => {
      await result.current.upload(new File(["hi"], "n.txt"));
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    });
    expect(result.current.notices).toEqual([]); // still processing

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    });

    expect(result.current.notices).toHaveLength(1);
    expect(result.current.notices[0]).toMatchObject({ kind: "success" });
    expect(result.current.notices[0].message).toContain("n.pdf");
    expect(result.current.notices[0].message).toMatch(/ready/);

    // ...and only once, even though the list keeps being refreshed
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 3);
    });
    expect(result.current.notices).toHaveLength(1);
  });

  it("explains a document that fails while it is being processed", async () => {
    vi.useFakeTimers();
    getDocumentsMock
      .mockResolvedValueOnce([])
      .mockResolvedValue([doc("n", "failed", 2)].map((d) => ({ ...d, error: "no_text" })));
    uploadMock.mockResolvedValue(doc("n", "pending", 2));

    const { result } = await ready();
    await act(async () => {
      await result.current.upload(new File(["hi"], "n.txt"));
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
    });

    expect(result.current.notices).toHaveLength(1);
    expect(result.current.notices[0].kind).toBe("error");
    expect(result.current.notices[0].message).toMatch(/could not be processed/);
    expect(result.current.notices[0].message).toMatch(/no readable text/i);
  });

  it("says so when the same file was already uploaded and ready", async () => {
    getDocumentsMock.mockResolvedValue([doc("n", "ready", 2)]);
    uploadMock.mockResolvedValue(doc("n", "ready", 2));

    const { result } = renderHook(() => useDocuments());
    await waitFor(() => expect(result.current.status).toBe("ready"));

    await act(async () => {
      await result.current.upload(new File(["hi"], "n.txt"));
    });

    expect(result.current.notices).toHaveLength(1);
    expect(result.current.notices[0]).toMatchObject({ kind: "info" });
    expect(result.current.notices[0].message).toMatch(/already uploaded/);
  });

  it("turns a network failure into words the user can act on", async () => {
    getDocumentsMock.mockResolvedValue([]);
    uploadMock.mockRejectedValue(
      new Error("Could not reach the server. Check your connection and try again."),
    );

    const { result } = renderHook(() => useDocuments());
    await waitFor(() => expect(result.current.status).toBe("ready"));

    await act(async () => {
      await result.current.upload(new File(["hi"], "n.txt"));
    });

    expect(result.current.error).toMatch(/n\.txt.*could not reach the server/i);
    expect(result.current.notices).toEqual([]);
  });

  it("shows the file name while it is being sent, and clears it after", async () => {
    getDocumentsMock.mockResolvedValue([]);
    let finish: (value: DocumentInfo) => void = () => {};
    uploadMock.mockReturnValue(new Promise((resolve) => (finish = resolve)));

    const { result } = renderHook(() => useDocuments());
    await waitFor(() => expect(result.current.status).toBe("ready"));

    let pending: Promise<void> = Promise.resolve();
    act(() => {
      pending = result.current.upload(new File(["hi"], "n.txt"));
    });

    expect(result.current.uploading).toBe(true);
    expect(result.current.uploadingNames).toEqual(["n.txt"]);

    await act(async () => {
      finish(doc("n", "pending", 2));
      await pending;
    });

    expect(result.current.uploading).toBe(false);
    expect(result.current.uploadingNames).toEqual([]);
  });

  it("two uploads at once: uploading stays true until both are done", async () => {
    getDocumentsMock.mockResolvedValue([]);
    const finishers: Array<(value: DocumentInfo) => void> = [];
    uploadMock.mockImplementation(
      () => new Promise((resolve) => finishers.push(resolve)),
    );

    const { result } = renderHook(() => useDocuments());
    await waitFor(() => expect(result.current.status).toBe("ready"));

    let first: Promise<void> = Promise.resolve();
    let second: Promise<void> = Promise.resolve();
    act(() => {
      first = result.current.upload(new File(["a"], "a.txt"));
      second = result.current.upload(new File(["b"], "b.txt"));
    });

    await act(async () => {
      finishers[0](doc("a", "ready", 2));
      await first;
    });
    expect(result.current.uploading).toBe(true);
    expect(result.current.uploadingNames).toEqual(["b.txt"]);

    await act(async () => {
      finishers[1](doc("b", "ready", 3));
      await second;
    });
    expect(result.current.uploading).toBe(false);
  });

  it("a list that was requested before the upload cannot hide the new document", async () => {
    let answerStale: (list: DocumentInfo[]) => void = () => {};
    getDocumentsMock
      .mockReturnValueOnce(new Promise((resolve) => (answerStale = resolve)))
      .mockResolvedValue([doc("n", "ready", 2)]);
    uploadMock.mockResolvedValue(doc("n", "ready", 2));

    const { result } = renderHook(() => useDocuments());

    await act(async () => {
      await result.current.upload(new File(["hi"], "n.txt"));
    });
    await act(async () => {
      answerStale([]); // the old answer: it does not know "n"
    });

    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(ids(result.current.documents)).toEqual(["n"]);
  });

  it("deleting an upload that is still processing reports nothing", async () => {
    vi.useFakeTimers();
    getDocumentsMock
      .mockResolvedValueOnce([])
      .mockResolvedValue([doc("n", "ready", 2)]);
    uploadMock.mockResolvedValue(doc("n", "pending", 2));
    deleteMock.mockResolvedValue(undefined);

    const { result } = await ready();
    await act(async () => {
      await result.current.upload(new File(["hi"], "n.txt"));
    });
    await act(async () => {
      await result.current.remove("n");
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * 2);
    });

    expect(result.current.notices).toEqual([]);
  });

  it("a notice can be dismissed", async () => {
    getDocumentsMock.mockResolvedValue([doc("n", "ready", 2)]);
    uploadMock.mockResolvedValue(doc("n", "ready", 2));

    const { result } = renderHook(() => useDocuments());
    await waitFor(() => expect(result.current.status).toBe("ready"));
    await act(async () => {
      await result.current.upload(new File(["hi"], "n.txt"));
    });

    act(() => result.current.dismissNotice(result.current.notices[0].id));

    expect(result.current.notices).toEqual([]);
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