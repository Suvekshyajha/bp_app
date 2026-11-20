import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RateLimitError, useChatSocket } from "./useChatSocket";
import { connectChatSocket } from "../services/chatSocket";
import type { SocketHandlers } from "../services/chatSocket";

vi.mock("../services/chatSocket", () => ({
  connectChatSocket: vi.fn(),
  sendChatRequest: vi.fn(),
}));

const connectMock = vi.mocked(connectChatSocket);

// Each call to connectChatSocket hands us the handlers the hook registered
let handlers: SocketHandlers;
let sockets: { close: ReturnType<typeof vi.fn> }[];

beforeEach(() => {
  vi.useFakeTimers();
  sockets = [];

  connectMock.mockImplementation((given) => {
    handlers = given;
    const socket = {
      readyState: WebSocket.OPEN,
      send: vi.fn(),
      close: vi.fn(),
      addEventListener: vi.fn(),
    };
    sockets.push(socket);
    return socket as unknown as WebSocket;
  });
});

afterEach(() => {
  vi.useRealTimers();
});

function connected(onAuthError?: () => void) {
  const hook = renderHook(() => useChatSocket(onAuthError));
  act(() => handlers.onOpen());
  return hook;
}

const message = (value: object) => JSON.stringify(value);

describe("connection status", () => {
  it("starts connecting and becomes connected on open", () => {
    const { result } = renderHook(() => useChatSocket());
    expect(result.current.status).toBe("connecting");

    act(() => handlers.onOpen());
    expect(result.current.status).toBe("connected");
  });

  it("reconnects with backoff after an ordinary close (e.g. heartbeat timeout 4001)", () => {
    const { result } = connected();

    act(() => handlers.onClose(new CloseEvent("close", { code: 4001 })));

    expect(result.current.status).toBe("reconnecting");
    expect(connectMock).toHaveBeenCalledTimes(1);

    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(connectMock).toHaveBeenCalledTimes(2);
  });

  it("shows offline once five attempts in a row have failed", () => {
    const { result } = connected();

    for (let i = 0; i < 5; i++) {
      act(() => handlers.onClose(new CloseEvent("close", { code: 1006 })));

      if (i < 4) {
        expect(result.current.status).toBe("reconnecting");
        // let the retry timer fire, so the next failure is a new attempt
        act(() => {
          vi.advanceTimersByTime(10_000);
        });
      }
    }

    expect(result.current.status).toBe("offline");
  });

  it("on 4401 stops retrying and tells the page to log out", () => {
    const onAuthError = vi.fn();
    const { result } = connected(onAuthError);

    act(() => handlers.onClose(new CloseEvent("close", { code: 4401 })));
    act(() => {
      vi.advanceTimersByTime(60_000);
    });

    expect(onAuthError).toHaveBeenCalledOnce();
    expect(result.current.status).toBe("offline");
    expect(connectMock).toHaveBeenCalledTimes(1); // no reconnect
  });

  it("on 4403 (origin blocked) stops retrying without logging out", () => {
    const onAuthError = vi.fn();
    connected(onAuthError);

    act(() => handlers.onClose(new CloseEvent("close", { code: 4403 })));
    act(() => {
      vi.advanceTimersByTime(60_000);
    });

    expect(onAuthError).not.toHaveBeenCalled();
    expect(connectMock).toHaveBeenCalledTimes(1);
  });

  it("closes the socket when the component unmounts", () => {
    const { unmount } = connected();

    unmount();

    expect(sockets[0].close).toHaveBeenCalledWith(1000, "Component unmounted");
  });
});

describe("send", () => {
  it("streams tokens, then resolves with the final answer", async () => {
    const { result } = connected();
    const onToken = vi.fn();

    const promise = result.current.send({ query: "hi", conversation_id: "c1" }, onToken);

    act(() => {
      handlers.onData(message({ type: "token", content: "Hel", conversation_id: "c1" }));
      handlers.onData(message({ type: "token", content: "lo", conversation_id: "c1" }));
      handlers.onData(
        message({ type: "done", query: "hi", answer: "Hello", conversation_id: "c1", results: [] }),
      );
    });

    await expect(promise).resolves.toMatchObject({ answer: "Hello", conversation_id: "c1" });
    expect(onToken.mock.calls.map((call) => call[0])).toEqual(["Hel", "lo"]);
  });

  it("rejects with RateLimitError, carrying retry_after, when the server says slow down", async () => {
    const { result } = connected();

    const promise = result.current.send({ query: "hi", conversation_id: "c1" });
    const assertion = expect(promise).rejects.toMatchObject({
      message: "You are sending messages too quickly. Please wait 7 seconds and try again.",
      retryAfter: 7,
    });

    act(() =>
      handlers.onData(
        message({
          error: "You are sending messages too quickly. Please wait 7 seconds and try again.",
          conversation_id: "c1",
          retry_after: 7,
        }),
      ),
    );

    await assertion;
    await expect(promise).rejects.toBeInstanceOf(RateLimitError);
  });

  it("a normal server error stays a plain Error (no cooldown)", async () => {
    const { result } = connected();

    const promise = result.current.send({ query: "hi", conversation_id: "c1" });
    const assertion = expect(promise).rejects.not.toBeInstanceOf(RateLimitError);

    act(() =>
      handlers.onData(message({ error: "Something went wrong.", conversation_id: "c1" })),
    );

    await assertion;
    await expect(promise).rejects.toThrow("Something went wrong.");
  });

  it("matches replies to the right chat when two are answering", async () => {
    const { result } = connected();
    const tokensA = vi.fn();
    const tokensB = vi.fn();

    const a = result.current.send({ query: "a", conversation_id: "chat-a" }, tokensA);
    const b = result.current.send({ query: "b", conversation_id: "chat-b" }, tokensB);

    act(() => {
      handlers.onData(message({ type: "token", content: "B1", conversation_id: "chat-b" }));
      handlers.onData(message({ type: "token", content: "A1", conversation_id: "chat-a" }));
      handlers.onData(message({ answer: "A", conversation_id: "chat-a", results: [] }));
      handlers.onData(message({ answer: "B", conversation_id: "chat-b", results: [] }));
    });

    await expect(a).resolves.toMatchObject({ answer: "A" });
    await expect(b).resolves.toMatchObject({ answer: "B" });
    expect(tokensA).toHaveBeenCalledWith("A1");
    expect(tokensB).toHaveBeenCalledWith("B1");
  });

  it("refuses a second request in the same chat", async () => {
    const { result } = connected();

    void result.current.send({ query: "one", conversation_id: "c1" }).catch(() => undefined);

    await expect(
      result.current.send({ query: "two", conversation_id: "c1" }),
    ).rejects.toThrow("already in progress");
  });

  it("rejects pending requests when the connection drops", async () => {
    const { result } = connected();

    const promise = result.current.send({ query: "hi", conversation_id: "c1" });
    const assertion = expect(promise).rejects.toThrow(/connection closed/i);

    act(() => handlers.onClose(new CloseEvent("close", { code: 4001 })));

    await assertion;
  });

  it("times out when the server goes quiet", async () => {
    const { result } = connected();

    const promise = result.current.send({ query: "hi", conversation_id: "c1" });
    const assertion = expect(promise).rejects.toThrow(/took too long/i);

    act(() => {
      vi.advanceTimersByTime(70_000);
    });

    await assertion;
  });

  it("each token restarts the timeout", async () => {
    const { result } = connected();
    const promise = result.current.send({ query: "hi", conversation_id: "c1" }, vi.fn());
    let settled = false;
    promise.then(() => (settled = true), () => (settled = true));

    for (let i = 0; i < 3; i++) {
      act(() => {
        vi.advanceTimersByTime(60_000);
      });
      act(() =>
        handlers.onData(message({ type: "token", content: "x", conversation_id: "c1" })),
      );
    }

    await Promise.resolve();
    expect(settled).toBe(false); // 180s in total, but never 70s of silence
  });

  it("rejects when there is no chat id", async () => {
    const { result } = connected();

    await expect(result.current.send({ query: "hi" })).rejects.toThrow("No chat is selected");
  });
});