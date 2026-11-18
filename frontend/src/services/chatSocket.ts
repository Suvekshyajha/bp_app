import { CHAT_SOCKET_URL } from "../config";
import type { ChatRequest } from "../types";

export interface SocketHandlers {
  onOpen: () => void;
  onData: (data: unknown) => void;
  onClose: (event: CloseEvent) => void;
  onError: () => void;
}

export function connectChatSocket(
  token: string,
  handlers: SocketHandlers,
): WebSocket {
  // URL() keeps the token attached correctly even if the base URL already has a query string.
  const url = new URL(CHAT_SOCKET_URL);
  url.searchParams.set("token", token);
  const socket = new WebSocket(url.toString());

  socket.addEventListener("open", handlers.onOpen);
  socket.addEventListener("message", (event: MessageEvent<unknown>) =>
    handlers.onData(event.data),
  );
  socket.addEventListener("close", (event: CloseEvent) =>
    handlers.onClose(event),
  );
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