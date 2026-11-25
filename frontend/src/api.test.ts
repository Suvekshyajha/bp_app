import { afterEach, describe, expect, it, vi } from "vitest";
import {
  NotFoundError,
  UnauthorizedError,
  deleteDocument,
  getConversations,
  getDocument,
  getDocuments,
  getMe,
  getMessages,
  uploadDocument,
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

describe("documents", () => {
  const server = {
    id: "d1",
    filename: "a.pdf",
    status: "ready",
    error: null,
    chunk_count: 4,
    created_at: "2026-11-01T10:00:00+00:00",
  };

  it("getDocuments maps the server's fields and sends the cookie", async () => {
    const fetchMock = mockFetch(200, [server]);

    const list = await getDocuments();

    expect(fetchMock).toHaveBeenCalledWith(
      API_BASE_URL + "/api/documents",
      expect.objectContaining({ credentials: "include" }),
    );
    expect(list).toEqual([
      {
        id: "d1",
        filename: "a.pdf",
        status: "ready",
        error: undefined,
        chunkCount: 4,
        createdAt: "2026-11-01T10:00:00+00:00",
      },
    ]);
  });

  it("getDocument encodes the id", async () => {
    const fetchMock = mockFetch(200, server);

    await getDocument("a/b");

    expect(fetchMock.mock.calls[0][0]).toBe(API_BASE_URL + "/api/documents/a%2Fb");
  });

  it("uploadDocument sends multipart form data with no hand-set Content-Type", async () => {
    const fetchMock = mockFetch(202, { id: "d2", filename: "n.txt", status: "pending" });

    const created = await uploadDocument(new File(["hello"], "n.txt"));

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(API_BASE_URL + "/api/documents");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("include");
    expect(init.body).toBeInstanceOf(FormData);
    expect((init.body as FormData).get("file")).toBeInstanceOf(File);
    // The browser must add the boundary itself
    expect(init.headers).toBeUndefined();
    expect(created).toMatchObject({ id: "d2", status: "pending" });
  });

  it("uploadDocument shows the server's reason for a refusal", async () => {
    mockFetch(413, { detail: "File is larger than 10 MB." });

    await expect(uploadDocument(new File(["x"], "big.pdf"))).rejects.toThrow(
      "File is larger than 10 MB.",
    );
  });

  it("deleteDocument sends DELETE", async () => {
    const fetchMock = mockFetch(204);

    await deleteDocument("d1");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(API_BASE_URL + "/api/documents/d1");
    expect(init.method).toBe("DELETE");
    expect(init.credentials).toBe("include");
  });

  it.each([
    ["getDocuments", () => getDocuments()],
    ["getDocument", () => getDocument("d1")],
    ["uploadDocument", () => uploadDocument(new File(["x"], "a.txt"))],
    ["deleteDocument", () => deleteDocument("d1")],
  ])("%s turns a 401 into UnauthorizedError", async (_name, call) => {
    mockFetch(401);

    await expect(call()).rejects.toBeInstanceOf(UnauthorizedError);
  });

  it("a 404 is NotFoundError for delete and for status", async () => {
    mockFetch(404);
    await expect(deleteDocument("d1")).rejects.toBeInstanceOf(NotFoundError);

    mockFetch(404);
    await expect(getDocument("d1")).rejects.toBeInstanceOf(NotFoundError);
  });
});
