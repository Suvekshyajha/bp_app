import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useConversations } from "./useConversations";
import { UnauthorizedError, getConversations } from "../api";
import type { Page, ServerConversation } from "../types";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  getConversations: vi.fn(),
}));

const getConversationsMock = vi.mocked(getConversations);

function chat(id: string, title: string | null = "Chat " + id): ServerConversation {
  return {
    id,
    title,
    created_at: "2026-11-01T10:00:00+00:00",
    updated_at: "2026-11-01T11:00:00+00:00",
  };
}

function page(ids: string[], next: string | null = null): Page<ServerConversation> {
  return { items: ids.map((id) => chat(id)), next_cursor: next };
}

const ids = (list: { id: string }[]) => list.map((c) => c.id);

function loaded(onAuthError?: () => void) {
  return renderHook(() => useConversations(onAuthError));
}

beforeEach(() => {
  localStorage.setItem(
    "day2.identity",
    JSON.stringify({ userId: "u1", displayName: "Alice", email: "a@example.com" }),
  );
  getConversationsMock.mockReset();
});

afterEach(() => {
  localStorage.clear();
});

describe("loading the list", () => {
  it("starts on a blank local chat while the list loads", () => {
    getConversationsMock.mockReturnValue(new Promise(() => undefined));

    const { result } = loaded();

    expect(result.current.status).toBe("loading");
    expect(result.current.conversations).toHaveLength(1);
    expect(result.current.conversations[0]).toMatchObject({ title: "New chat", isLocal: true });
    expect(result.current.selectedId).toBe(result.current.conversations[0].id);
  });

  it("puts the server's chats under the blank chat", async () => {
    getConversationsMock.mockResolvedValue(page(["s1", "s2"]));

    const { result } = loaded();
    await waitFor(() => expect(result.current.status).toBe("ready"));

    expect(ids(result.current.conversations).slice(1)).toEqual(["s1", "s2"]);
    expect(result.current.conversations[0].isLocal).toBe(true);
    expect(result.current.conversations[1]).toMatchObject({
      title: "Chat s1",
      createdAt: "2026-11-01T10:00:00+00:00",
      updatedAt: "2026-11-01T11:00:00+00:00",
    });
  });

  it("uses 'New chat' when the server title is null or blank", async () => {
    getConversationsMock.mockResolvedValue({
      items: [chat("s1", null), chat("s2", "   ")],
      next_cursor: null,
    });

    const { result } = loaded();
    await waitFor(() => expect(result.current.status).toBe("ready"));

    expect(result.current.conversations.slice(1).map((c) => c.title)).toEqual([
      "New chat",
      "New chat",
    ]);
  });

  it("reopens the chat that was open last time", async () => {
    localStorage.setItem("day2.selected.u1", JSON.stringify("s2"));
    getConversationsMock.mockResolvedValue(page(["s1", "s2"]));

    const { result } = loaded();
    await waitFor(() => expect(result.current.selectedId).toBe("s2"));
  });

  it("stays on the blank chat if the saved chat no longer exists", async () => {
    localStorage.setItem("day2.selected.u1", JSON.stringify("gone"));
    getConversationsMock.mockResolvedValue(page(["s1"]));

    const { result } = loaded();
    await waitFor(() => expect(result.current.status).toBe("ready"));

    expect(result.current.conversations.find((c) => c.id === result.current.selectedId)?.isLocal).toBe(true);
  });

  it("remembers the selected chat for next time", async () => {
    getConversationsMock.mockResolvedValue(page(["s1"]));

    const { result } = loaded();
    await waitFor(() => expect(result.current.status).toBe("ready"));
    act(() => result.current.selectConversation("s1"));

    await waitFor(() =>
      expect(JSON.parse(localStorage.getItem("day2.selected.u1")!)).toBe("s1"),
    );
  });

  it("shows an error and retryLoad recovers", async () => {
    getConversationsMock.mockRejectedValueOnce(new Error("HTTP 500"));
    getConversationsMock.mockResolvedValueOnce(page(["s1"]));

    const { result } = loaded();
    await waitFor(() => expect(result.current.status).toBe("error"));

    act(() => result.current.retryLoad());
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(ids(result.current.conversations)).toContain("s1");
  });

  it("a 401 calls onAuthError", async () => {
    getConversationsMock.mockRejectedValue(new UnauthorizedError());
    const onAuthError = vi.fn();

    loaded(onAuthError);

    await waitFor(() => expect(onAuthError).toHaveBeenCalledOnce());
  });
});

describe("load more", () => {
  it("hasMore follows next_cursor", async () => {
    getConversationsMock.mockResolvedValue(page(["s1"], "cursor-1"));

    const { result } = loaded();
    await waitFor(() => expect(result.current.status).toBe("ready"));

    expect(result.current.hasMore).toBe(true);
  });

  it("appends the next page using the cursor, and stops on the last page", async () => {
    getConversationsMock.mockResolvedValueOnce(page(["s1", "s2"], "cursor-1"));
    getConversationsMock.mockResolvedValueOnce(page(["s3"], null));

    const { result } = loaded();
    await waitFor(() => expect(result.current.hasMore).toBe(true));

    await act(async () => {
      await result.current.loadMore();
    });

    expect(getConversationsMock).toHaveBeenLastCalledWith("cursor-1");
    expect(ids(result.current.conversations).slice(1)).toEqual(["s1", "s2", "s3"]);
    expect(result.current.hasMore).toBe(false);
  });

  it("does nothing when there is no next page", async () => {
    getConversationsMock.mockResolvedValue(page(["s1"], null));

    const { result } = loaded();
    await waitFor(() => expect(result.current.status).toBe("ready"));

    await act(async () => {
      await result.current.loadMore();
    });

    expect(getConversationsMock).toHaveBeenCalledTimes(1);
  });

  it("does not add a chat that is already in the list", async () => {
    getConversationsMock.mockResolvedValueOnce(page(["s1", "s2"], "cursor-1"));
    getConversationsMock.mockResolvedValueOnce(page(["s2", "s3"], null));

    const { result } = loaded();
    await waitFor(() => expect(result.current.hasMore).toBe(true));
    await act(async () => {
      await result.current.loadMore();
    });

    expect(ids(result.current.conversations).slice(1)).toEqual(["s1", "s2", "s3"]);
  });

  it("keeps the button when loading fails, so the user can retry", async () => {
    getConversationsMock.mockResolvedValueOnce(page(["s1"], "cursor-1"));
    getConversationsMock.mockRejectedValueOnce(new Error("offline"));

    const { result } = loaded();
    await waitFor(() => expect(result.current.hasMore).toBe(true));
    await act(async () => {
      await result.current.loadMore();
    });

    expect(result.current.hasMore).toBe(true);
    expect(result.current.loadingMore).toBe(false);
  });
});

describe("touchConversation", () => {
  it("uses the first message as the title (trimmed to 40 characters)", async () => {
    getConversationsMock.mockResolvedValue(page([]));
    const { result } = loaded();
    await waitFor(() => expect(result.current.status).toBe("ready"));
    const id = result.current.selectedId;

    act(() => result.current.touchConversation(id, "  hello \n  there  " + "x".repeat(60)));

    const title = result.current.conversations[0].title;
    expect(title.startsWith("hello there x")).toBe(true);
    expect(title).toHaveLength(40);
  });

  it("does not rename a chat that already has a real title", async () => {
    getConversationsMock.mockResolvedValue(page(["s1"]));
    const { result } = loaded();
    await waitFor(() => expect(result.current.status).toBe("ready"));

    act(() => result.current.touchConversation("s1", "a new first message"));

    expect(result.current.conversations.find((c) => c.id === "s1")?.title).toBe("Chat s1");
  });

  it("moves the chat to the top of the list", async () => {
    getConversationsMock.mockResolvedValue(page(["s1", "s2", "s3"]));
    const { result } = loaded();
    await waitFor(() => expect(result.current.status).toBe("ready"));

    act(() => result.current.touchConversation("s3"));

    expect(result.current.conversations[0].id).toBe("s3");
    expect(result.current.conversations).toHaveLength(4);
  });

  it("ignores a chat that is not in the list", async () => {
    getConversationsMock.mockResolvedValue(page(["s1"]));
    const { result } = loaded();
    await waitFor(() => expect(result.current.status).toBe("ready"));
    const before = result.current.conversations;

    act(() => result.current.touchConversation("unknown"));

    expect(result.current.conversations).toBe(before);
  });
});

describe("markSynced and createConversation", () => {
  it("markSynced clears isLocal", async () => {
    getConversationsMock.mockResolvedValue(page([]));
    const { result } = loaded();
    await waitFor(() => expect(result.current.status).toBe("ready"));
    const id = result.current.selectedId;

    act(() => result.current.markSynced(id));

    expect(result.current.conversations[0].isLocal).toBe(false);
  });

  it("createConversation adds a local chat on top and selects it", async () => {
    getConversationsMock.mockResolvedValue(page(["s1"]));
    const { result } = loaded();
    await waitFor(() => expect(result.current.status).toBe("ready"));

    let newId = "";
    act(() => {
      newId = result.current.createConversation();
    });

    expect(result.current.selectedId).toBe(newId);
    expect(result.current.conversations[0]).toMatchObject({ id: newId, isLocal: true });
  });
});

describe("deleteConversation (hides the chat in this browser)", () => {
  it("removes the chat and selects another one", async () => {
    getConversationsMock.mockResolvedValue(page(["s1", "s2"]));
    const { result } = loaded();
    await waitFor(() => expect(result.current.status).toBe("ready"));
    act(() => result.current.selectConversation("s1"));

    act(() => result.current.deleteConversation("s1"));

    expect(ids(result.current.conversations)).not.toContain("s1");
    expect(result.current.selectedId).not.toBe("s1");
    expect(ids(result.current.conversations)).toContain(result.current.selectedId);
  });

  it("deleting the only chat leaves a fresh blank chat", async () => {
    getConversationsMock.mockResolvedValue(page([]));
    const { result } = loaded();
    await waitFor(() => expect(result.current.status).toBe("ready"));
    const only = result.current.selectedId;

    act(() => result.current.deleteConversation(only));

    expect(result.current.conversations).toHaveLength(1);
    expect(result.current.conversations[0].id).not.toBe(only);
    expect(result.current.conversations[0].isLocal).toBe(true);
  });

  it("stays hidden after a reload, because the server still returns it", async () => {
    getConversationsMock.mockResolvedValue(page(["s1", "s2"]));
    const first = loaded();
    await waitFor(() => expect(first.result.current.status).toBe("ready"));
    act(() => first.result.current.deleteConversation("s1"));
    first.unmount();

    const second = loaded(); // "reload"
    await waitFor(() => expect(second.result.current.status).toBe("ready"));

    expect(ids(second.result.current.conversations)).not.toContain("s1");
    expect(ids(second.result.current.conversations)).toContain("s2");
  });

  it("hidden chats are also filtered out of later pages", async () => {
    localStorage.setItem("day2.hidden.u1", JSON.stringify(["s2"]));
    getConversationsMock.mockResolvedValueOnce(page(["s1"], "cursor-1"));
    getConversationsMock.mockResolvedValueOnce(page(["s2", "s3"], null));

    const { result } = loaded();
    await waitFor(() => expect(result.current.hasMore).toBe(true));
    await act(async () => {
      await result.current.loadMore();
    });

    expect(ids(result.current.conversations).slice(1)).toEqual(["s1", "s3"]);
  });
});