import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { connectChatSocket, sendChatRequest } from "./chatSocket";
import type { SocketHandlers } from "./chatSocket";

// A WebSocket we can drive by hand
class FakeSocket extends EventTarget {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  static last: FakeSocket;

  readyState = FakeSocket.CONNECTING;
  sent: string[] = [];
  closeCall: { code?: number; reason?: string } | null = null;

  constructor() {
    super();
    FakeSocket.last = this;
  }

  send(data: string) {
    this.sent.push(data);
  }

  close(code?: number, reason?: string) {
    this.closeCall = { code, reason };
    this.readyState = FakeSocket.CLOSED;
  }

  // helpers for tests
  open() {
    this.readyState = FakeSocket.OPEN;
    this.dispatchEvent(new Event("open"));
  }

  receive(data: unknown) {
    this.dispatchEvent(new MessageEvent("message", { data }));
  }

  serverClose(code: number) {
    this.readyState = FakeSocket.CLOSED;
    this.dispatchEvent(new CloseEvent("close", { code }));
  }
}

const INTERVAL = 1000;
const TIMEOUT = 400;

function setup(options = { intervalMs: INTERVAL, timeoutMs: TIMEOUT }) {
  const handlers = {
    onOpen: vi.fn(),
    onData: vi.fn(),
    onClose: vi.fn(),
    onError: vi.fn(),
  } satisfies SocketHandlers;

  connectChatSocket(handlers, options);

  return { handlers, socket: FakeSocket.last };
}

const pings = (socket: FakeSocket) =>
  socket.sent.filter((text) => JSON.parse(text).type === "ping");

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("WebSocket", FakeSocket);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("heartbeat", () => {
  it("does not ping before the socket is open", () => {
    const { socket } = setup();

    vi.advanceTimersByTime(INTERVAL * 3);

    expect(socket.sent).toEqual([]);
  });

  it("sends a ping every interval once open", () => {
    const { socket, handlers } = setup();
    socket.open();

    vi.advanceTimersByTime(INTERVAL);
    expect(pings(socket)).toHaveLength(1);

    socket.receive(JSON.stringify({ type: "pong" })); // answer it
    vi.advanceTimersByTime(INTERVAL);
    expect(pings(socket)).toHaveLength(2);

    expect(handlers.onOpen).toHaveBeenCalledOnce();
  });

  it("keeps the connection when the pong arrives in time", () => {
    const { socket, handlers } = setup();
    socket.open();

    vi.advanceTimersByTime(INTERVAL);          // ping sent
    vi.advanceTimersByTime(TIMEOUT - 1);       // just before the deadline
    socket.receive(JSON.stringify({ type: "pong" }));
    vi.advanceTimersByTime(INTERVAL - TIMEOUT); // up to (not past) the next ping

    expect(handlers.onClose).not.toHaveBeenCalled();
    expect(socket.closeCall).toBeNull();
  });

  it("does not pass pongs on to the chat code", () => {
    const { socket, handlers } = setup();
    socket.open();

    socket.receive(JSON.stringify({ type: "pong" }));

    expect(handlers.onData).not.toHaveBeenCalled();
  });

  it("gives up with code 4001 when no pong comes", () => {
    const { socket, handlers } = setup();
    socket.open();

    vi.advanceTimersByTime(INTERVAL + TIMEOUT);

    expect(socket.closeCall).toEqual({ code: 4001, reason: "Heartbeat timeout" });
    expect(handlers.onClose).toHaveBeenCalledOnce();
    expect(handlers.onClose.mock.calls[0][0].code).toBe(4001);
  });

  it("reports the close only once, even if the browser closes later too", () => {
    const { socket, handlers } = setup();
    socket.open();
    vi.advanceTimersByTime(INTERVAL + TIMEOUT);

    socket.serverClose(1006); // the late real close event

    expect(handlers.onClose).toHaveBeenCalledOnce();
  });

  it("does not send a second ping while still waiting for the first", () => {
    // A timeout longer than the interval, so the interval fires again
    // while the first ping is still unanswered
    const { socket } = setup({ intervalMs: INTERVAL, timeoutMs: INTERVAL * 10 });
    socket.open();

    vi.advanceTimersByTime(INTERVAL * 4);

    expect(pings(socket)).toHaveLength(1);
  });

  it("treats any server message as proof the connection is alive", () => {
    const { socket, handlers } = setup();
    socket.open();

    vi.advanceTimersByTime(INTERVAL);          // ping sent, waiting
    socket.receive(JSON.stringify({ type: "token", content: "hi" }));
    vi.advanceTimersByTime(TIMEOUT * 3);

    expect(handlers.onClose).not.toHaveBeenCalled();
    expect(handlers.onData).toHaveBeenCalledOnce();
  });

  it("stops pinging after a normal close", () => {
    const { socket, handlers } = setup();
    socket.open();

    socket.serverClose(1000);
    vi.advanceTimersByTime(INTERVAL * 5);

    expect(pings(socket)).toHaveLength(0);
    expect(handlers.onClose).toHaveBeenCalledOnce();
  });
});

describe("incoming messages", () => {
  it("forwards chat data untouched", () => {
    const { socket, handlers } = setup();
    socket.open();

    const raw = JSON.stringify({ type: "done", answer: "Hello" });
    socket.receive(raw);

    expect(handlers.onData).toHaveBeenCalledWith(raw);
  });

  it("only treats type=pong as a pong", () => {
    const { socket, handlers } = setup();
    socket.open();

    // short enough to be parsed, but not a pong
    socket.receive(JSON.stringify({ type: "token", content: "pong" }));
    socket.receive("not json");

    expect(handlers.onData).toHaveBeenCalledTimes(2);
  });

  it("forwards the server's close code (e.g. 4401 = log in again)", () => {
    const { socket, handlers } = setup();
    socket.open();

    socket.serverClose(4401);

    expect(handlers.onClose.mock.calls[0][0].code).toBe(4401);
  });
});

describe("sendChatRequest", () => {
  it("sends the request as JSON on an open socket", () => {
    const { socket } = setup();
    socket.open();

    sendChatRequest(socket as unknown as WebSocket, {
      query: "hi",
      conversation_id: "c1",
    });

    expect(JSON.parse(socket.sent[0])).toEqual({ query: "hi", conversation_id: "c1" });
  });

  it("throws instead of sending when the socket is not open", () => {
    const { socket } = setup(); // still connecting

    expect(() =>
      sendChatRequest(socket as unknown as WebSocket, { query: "hi" }),
    ).toThrow(/not ready/i);
    expect(socket.sent).toEqual([]);
  });
});

describe("document_ids", () => {
  it("are sent with the question when documents are ticked", () => {
    const { socket } = setup();
    socket.open();

    sendChatRequest(socket as unknown as WebSocket, {
      query: "what does the report say?",
      conversation_id: "c1",
      document_ids: ["d1", "d2"],
    });

    expect(JSON.parse(socket.sent[0])).toEqual({
      query: "what does the report say?",
      conversation_id: "c1",
      document_ids: ["d1", "d2"],
    });
  });

  it("are absent when no document is ticked", () => {
    const { socket } = setup();
    socket.open();

    sendChatRequest(socket as unknown as WebSocket, { query: "hi" });

    expect(JSON.parse(socket.sent[0])).not.toHaveProperty("document_ids");
  });
});
