
import { CHAT_SOCKET_URL } from "../config";
import type { ChatRequest } from "../types";

export interface SocketHandlers {
  onOpen: () => void;
  onData: (data: unknown) => void;
  onClose: () => void;
  onError: () => void;
}

export function connectChatSocket(
  token: string,
  handlers: SocketHandlers,
): WebSocket {
  const socketUrl = `${CHAT_SOCKET_URL}?token=${encodeURIComponent(token)}`;
  const socket = new WebSocket(socketUrl);

  socket.addEventListener("open", handlers.onOpen);
  socket.addEventListener("message", (event: MessageEvent<unknown>) =>
    handlers.onData(event.data),
  );
  socket.addEventListener("close", handlers.onClose);
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
