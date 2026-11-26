import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useChatMessages } from "./useChatMessages";
import { UnauthorizedError, getMessages } from "../api";
import type { ChatMessage, Page, ServerMessage } from "../types";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  getMessages: vi.fn(),
}));

const getMessagesMock = vi.mocked(getMessages);

function serverMessage(id: string, role: "user" | "assistant" = "user"): ServerMessage {
  return { id, role, content: "text " + id, created_at: "2026-11-01T12:00:00+00:00" };
}

function page(ids: string[], next: string | null = null): Page<ServerMessage> {
  return { items: ids.map((id) => serverMessage(id)), next_cursor: next };
}

function localMessage(id: string): ChatMessage {
  return { id, role: "user", content: "local " + id, createdAt: "2026-11-01T13:00:00Z", status: "pending" };
}

// A promise the test resolves by hand, to control when the server "answers"
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

const ids = (messages: ChatMessage[]) => messages.map((m) => m.id);

beforeEach(() => {
  getMessagesMock.mockReset();
});

describe("loading the latest page", () => {
  it("loads when a saved chat is opened, and maps server messages", async () => {
    getMessagesMock.mockResolvedValue({
      items: [serverMessage("m1", "user"), serverMessage("m2", "assistant")],
      next_cursor: null,
    });

    const { result } = renderHook(() => useChatMessages("chat-a", false));

    expect(result.current.historyStatus).toBe("loading");

    await waitFor(() => expect(result.current.historyStatus).toBe("ready"));

    expect(getMessagesMock).toHaveBeenCalledWith("chat-a");
    expect(result.current.messages).toEqual([
      expect.objectContaining({ id: "m1", role: "user", status: "sent", createdAt: "2026-11-01T12:00:00+00:00" }),
      expect.objectContaining({ id: "m2", role: "assistant", status: undefined }),
    ]);
  });

  it("does not call the server for a brand-new local chat", () => {
    const { result } = renderHook(() => useChatMessages("new-chat", true));

    expect(result.current.historyStatus).toBe("ready");
    expect(getMessagesMock).not.toHaveBeenCalled();
  });

  it("does not load again when a local chat becomes synced", () => {
    const { rerender } = renderHook(
      ({ local }) => useChatMessages("chat-a", local),
      { initialProps: { local: true } },
    );

    rerender({ local: false }); // markSynced after the first message

    expect(getMessagesMock).not.toHaveBeenCalled();
  });

  it("loads each chat only once, even when switching back and forth", async () => {
    getMessagesMock.mockResolvedValue(page(["m1"]));

    const { result, rerender } = renderHook(
      ({ id }) => useChatMessages(id, false),
      { initialProps: { id: "chat-a" } },
    );
    await waitFor(() => expect(result.current.historyStatus).toBe("ready"));

    rerender({ id: "chat-b" });
    await waitFor(() => expect(result.current.historyStatus).toBe("ready"));
    rerender({ id: "chat-a" });

    expect(getMessagesMock).toHaveBeenCalledTimes(2);
    expect(result.current.messages).toHaveLength(1); // chat-a kept in memory
  });

  it("keeps messages added while loading, with the older server messages in front", async () => {
    const request = deferred<Page<ServerMessage>>();
    getMessagesMock.mockReturnValue(request.promise);

    const { result } = renderHook(() => useChatMessages("chat-a", false));

    act(() => result.current.addMessage("chat-a", localMessage("new")));
    await act(async () => request.resolve(page(["old1", "old2"])));

    expect(ids(result.current.messages)).toEqual(["old1", "old2", "new"]);
  });
});

describe("load errors", () => {
  it("shows an error, and retryLoad recovers", async () => {
    getMessagesMock.mockRejectedValueOnce(new Error("HTTP 500"));
    getMessagesMock.mockResolvedValueOnce(page(["m1"]));

    const { result } = renderHook(() => useChatMessages("chat-a", false));
    await waitFor(() => expect(result.current.historyStatus).toBe("error"));

    act(() => result.current.retryLoad());
    expect(result.current.historyStatus).toBe("loading");

    await waitFor(() => expect(result.current.historyStatus).toBe("ready"));
    expect(ids(result.current.messages)).toEqual(["m1"]);
  });

  it("a 401 calls onAuthError so the app can log out", async () => {
    getMessagesMock.mockRejectedValue(new UnauthorizedError());
    const onAuthError = vi.fn();

    renderHook(() => useChatMessages("chat-a", false, onAuthError));

    await waitFor(() => expect(onAuthError).toHaveBeenCalledOnce());
  });
});

describe("loading earlier messages", () => {
  it("has nothing earlier when next_cursor is null", async () => {
    getMessagesMock.mockResolvedValue(page(["m1"], null));

    const { result } = renderHook(() => useChatMessages("chat-a", false));
    await waitFor(() => expect(result.current.historyStatus).toBe("ready"));

    expect(result.current.hasEarlier).toBe(false);
  });

  it("uses the cursor and puts older messages before the current ones", async () => {
    getMessagesMock.mockResolvedValueOnce(page(["m3", "m4"], "cursor-1"));
    getMessagesMock.mockResolvedValueOnce(page(["m1", "m2"], null));

    const { result } = renderHook(() => useChatMessages("chat-a", false));
    await waitFor(() => expect(result.current.hasEarlier).toBe(true));

    await act(async () => {
      await result.current.loadEarlier();
    });

    expect(getMessagesMock).toHaveBeenLastCalledWith("chat-a", "cursor-1");
    expect(ids(result.current.messages)).toEqual(["m1", "m2", "m3", "m4"]);
    expect(result.current.hasEarlier).toBe(false);
  });

  it("shows loadingEarlier while waiting, and ignores a second click", async () => {
    getMessagesMock.mockResolvedValueOnce(page(["m3"], "cursor-1"));
    const older = deferred<Page<ServerMessage>>();
    getMessagesMock.mockReturnValueOnce(older.promise);

    const { result } = renderHook(() => useChatMessages("chat-a", false));
    await waitFor(() => expect(result.current.hasEarlier).toBe(true));

    act(() => {
      void result.current.loadEarlier();
    });
    await waitFor(() => expect(result.current.loadingEarlier).toBe(true));

    act(() => {
      void result.current.loadEarlier(); // double click
    });
    expect(getMessagesMock).toHaveBeenCalledTimes(2); // first page + one earlier request

    await act(async () => older.resolve(page(["m2"])));
    expect(result.current.loadingEarlier).toBe(false);
  });

  it("does not duplicate a message that is already shown", async () => {
    getMessagesMock.mockResolvedValueOnce(page(["m2", "m3"], "cursor-1"));
    getMessagesMock.mockResolvedValueOnce(page(["m1", "m2"], null)); // overlaps on m2

    const { result } = renderHook(() => useChatMessages("chat-a", false));
    await waitFor(() => expect(result.current.hasEarlier).toBe(true));
    await act(async () => {
      await result.current.loadEarlier();
    });

    expect(ids(result.current.messages)).toEqual(["m1", "m2", "m3"]);
  });

  it("keeps the button available when loading earlier messages fails", async () => {
    getMessagesMock.mockResolvedValueOnce(page(["m2"], "cursor-1"));
    getMessagesMock.mockRejectedValueOnce(new Error("offline"));

    const { result } = renderHook(() => useChatMessages("chat-a", false));
    await waitFor(() => expect(result.current.hasEarlier).toBe(true));
    await act(async () => {
      await result.current.loadEarlier();
    });

    expect(result.current.loadingEarlier).toBe(false);
    expect(result.current.hasEarlier).toBe(true); // can try again
    expect(ids(result.current.messages)).toEqual(["m2"]);
  });
});

describe("editing messages (used while streaming a reply)", () => {
  it("adds, updates and removes messages", () => {
    const { result } = renderHook(() => useChatMessages("chat-a", true));

    act(() => result.current.addMessage("chat-a", localMessage("u1")));
    act(() =>
      result.current.updateMessage("chat-a", "u1", (m) => ({ ...m, status: "sent" })),
    );
    expect(result.current.messages[0].status).toBe("sent");

    act(() => result.current.removeMessage("chat-a", "u1"));
    expect(result.current.messages).toEqual([]);
  });

  it("can update a chat that is not the selected one (background answer)", () => {
    const { result, rerender } = renderHook(
      ({ id }) => useChatMessages(id, true),
      { initialProps: { id: "chat-a" } },
    );

    act(() => result.current.addMessage("chat-a", localMessage("a1")));
    rerender({ id: "chat-b" });
    act(() => result.current.addMessage("chat-a", localMessage("a2"))); // token for chat-a

    expect(result.current.messages).toEqual([]); // chat-b is still empty
    rerender({ id: "chat-a" });
    expect(ids(result.current.messages)).toEqual(["a1", "a2"]);
  });
});

describe("removeChat", () => {
  it("drops the messages and ignores late replies for that chat", () => {
    const { result } = renderHook(() => useChatMessages("chat-a", true));
    act(() => result.current.addMessage("chat-a", localMessage("a1")));

    act(() => result.current.removeChat("chat-a"));
    act(() => result.current.addMessage("chat-a", localMessage("late")));

    expect(result.current.messages).toEqual([]);
  });

  it("a history response that arrives after deletion does not bring the chat back", async () => {
    const request = deferred<Page<ServerMessage>>();
    getMessagesMock.mockReturnValue(request.promise);

    const { result } = renderHook(() => useChatMessages("chat-a", false));
    act(() => result.current.removeChat("chat-a"));
    await act(async () => request.resolve(page(["m1"])));

    expect(result.current.messages).toEqual([]);
  });
});

describe("saved sources", () => {
  it("keeps the sources an answer was written with when a chat is loaded", async () => {
    const source = {
      id: "d1:0",
      payload: { document_id: "d1", filename: "report.pdf", chunk_index: 0, content: "Revenue grew." },
    };

    getMessagesMock.mockResolvedValue({
      items: [serverMessage("u1", "user"), { ...serverMessage("a1", "assistant"), results: [source] }],
      next_cursor: null,
    });

    const { result } = renderHook(() => useChatMessages("chat-a", false));
    await waitFor(() => expect(result.current.historyStatus).toBe("ready"));

    expect(result.current.messages[1].context).toEqual([source]);
  });

  it("gives a message with no sources no context", async () => {
    getMessagesMock.mockResolvedValue({
      items: [{ ...serverMessage("a1", "assistant"), results: [] }, serverMessage("u1", "user")],
      next_cursor: null,
    });

    const { result } = renderHook(() => useChatMessages("chat-a", false));
    await waitFor(() => expect(result.current.historyStatus).toBe("ready"));

    expect(result.current.messages[0].context).toBeUndefined();
    expect(result.current.messages[1].context).toBeUndefined();
  });
});
