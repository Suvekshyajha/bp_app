import { CHAT_SOCKET_URL } from "../config";
import type { ChatRequest } from "../types";

export interface SocketHandlers {
  onOpen: () => void;
  onData: (data: unknown) => void;
  onClose: (event: CloseEvent) => void;
  onError: () => void;
}

export interface HeartbeatOptions {
  intervalMs?: number; // how often to send a ping
  timeoutMs?: number; // how long to wait for any sign of life before giving up
}

const HEARTBEAT_INTERVAL_MS = 25_000;
const HEARTBEAT_TIMEOUT_MS = 10_000;
const WS_HEARTBEAT_TIMEOUT = 4001;

// The server answers a ping with {"type":"pong"}
function isPong(raw: unknown): boolean {
  // A pong is tiny, so skip parsing the long messages
  if (typeof raw !== "string" || raw.length > 40) return false;

  try {
    const value: unknown = JSON.parse(raw);
    return (
      typeof value === "object" &&
      value !== null &&
      (value as Record<string, unknown>).type === "pong"
    );
  } catch {
    return false;
  }
}

export function connectChatSocket(
  handlers: SocketHandlers,
  heartbeat: HeartbeatOptions = {},
): WebSocket {
  const intervalMs = heartbeat.intervalMs ?? HEARTBEAT_INTERVAL_MS;
  const timeoutMs = heartbeat.timeoutMs ?? HEARTBEAT_TIMEOUT_MS;

  // No token here: the browser sends the session cookie by itself.
  const socket = new WebSocket(CHAT_SOCKET_URL);

  let pingTimer: number | undefined;
  let pongTimer: number | undefined;
  let finished = false; // we already reported this socket as closed

  const stopHeartbeat = () => {
    window.clearInterval(pingTimer);
    window.clearTimeout(pongTimer);
    pingTimer = undefined;
    pongTimer = undefined;
  };

  // Give up on a socket that stopped answering. A dead connection can take
  // minutes to report "close" by itself, so we report it right away.
  const giveUp = () => {
    finished = true;
    stopHeartbeat();

    try {
      socket.close(WS_HEARTBEAT_TIMEOUT, "Heartbeat timeout");
    } catch {
      // already closed
    }

    handlers.onClose(
      new CloseEvent("close", {
        code: WS_HEARTBEAT_TIMEOUT,
        reason: "Heartbeat timeout",
      }),
    );
  };

  const startHeartbeat = () => {
    stopHeartbeat();

    pingTimer = window.setInterval(() => {
      if (socket.readyState !== WebSocket.OPEN) return;

      // Already waiting for an answer
      if (pongTimer !== undefined) return;

      socket.send(JSON.stringify({ type: "ping" }));
      pongTimer = window.setTimeout(giveUp, timeoutMs);
    }, intervalMs);
  };

  socket.addEventListener("open", () => {
    startHeartbeat();
    handlers.onOpen();
  });

  socket.addEventListener("message", (event: MessageEvent<unknown>) => {
    if (finished) return;

    // Any message from the server proves the connection is alive
    window.clearTimeout(pongTimer);
    pongTimer = undefined;

    // Heartbeat answers are not chat data
    if (isPong(event.data)) return;

    handlers.onData(event.data);
  });

  socket.addEventListener("close", (event: CloseEvent) => {
    stopHeartbeat();

    if (finished) return; // giveUp() already told the caller

    finished = true;
    handlers.onClose(event);
  });

  socket.addEventListener("error", handlers.onError);

  return socket;
}

export function sendChatRequest(
  socket: WebSocket,
  request: ChatRequest,
): void {
  if (socket.readyState !== WebSocket.OPEN) {
    throw new Error("The chat connection is not ready. Try again in a moment.");
  }

  socket.send(JSON.stringify(request));
}