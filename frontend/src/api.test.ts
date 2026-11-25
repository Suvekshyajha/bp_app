import { afterEach, describe, expect, it, vi } from "vitest";
import {
  NotFoundError,
  UnauthorizedError,
  getConversations,
  getMe,
  getMessages,
} from "./api";
import { API_BASE_URL } from "./config";

function mockFetch(status: number, body: unknown = {}) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("getConversations", () => {
  it("asks for the first page with no query string and sends the cookie", async () => {
    const fetchMock = mockFetch(200, { items: [], next_cursor: null });

    await getConversations();

    expect(fetchMock).toHaveBeenCalledWith(
      API_BASE_URL + "/api/conversations",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("sends limit and cursor, URL-encoding the cursor", async () => {
    const fetchMock = mockFetch(200, { items: [], next_cursor: null });

    // Real cursors are base64 and end in "=", which must not break the URL
    await getConversations("abc==", 5);

    const url = new URL(fetchMock.mock.calls[0][0] as string);
    expect(url.searchParams.get("limit")).toBe("5");
    expect(url.searchParams.get("cursor")).toBe("abc==");
  });

  it("returns the page the server sent", async () => {
    const page = { items: [{ id: "c1", title: "Hi" }], next_cursor: "next" };
    mockFetch(200, page);

    await expect(getConversations()).resolves.toEqual(page);
  });
});

describe("getMessages", () => {
  it("builds the per-chat URL", async () => {
    const fetchMock = mockFetch(200, { items: [], next_cursor: null });

    await getMessages("chat-1");

    expect(fetchMock.mock.calls[0][0]).toBe(
      API_BASE_URL + "/api/conversations/chat-1/messages",
    );
  });

  it("encodes the conversation id so it cannot change the path", async () => {
    const fetchMock = mockFetch(200, { items: [], next_cursor: null });

    await getMessages("a/b?x=1");

    expect(fetchMock.mock.calls[0][0]).toBe(
      API_BASE_URL + "/api/conversations/a%2Fb%3Fx%3D1/messages",
    );
  });

  it("passes the cursor for older messages", async () => {
    const fetchMock = mockFetch(200, { items: [], next_cursor: null });

    await getMessages("chat-1", "older-cursor");

    const url = new URL(fetchMock.mock.calls[0][0] as string);
    expect(url.searchParams.get("cursor")).toBe("older-cursor");
  });
});

describe("error handling", () => {
  it("401 becomes UnauthorizedError, so the app can send the user to login", async () => {
    mockFetch(401);

    await expect(getConversations()).rejects.toBeInstanceOf(UnauthorizedError);
    await expect(getMessages("c")).rejects.toBeInstanceOf(UnauthorizedError);
    await expect(getMe()).rejects.toBeInstanceOf(UnauthorizedError);
  });

  it("404 becomes NotFoundError", async () => {
    mockFetch(404);

    await expect(getMessages("missing")).rejects.toBeInstanceOf(NotFoundError);
  });

  it("other failures keep the HTTP status in the message", async () => {
    mockFetch(500);

    await expect(getConversations()).rejects.toThrow("HTTP 500");
  });
});