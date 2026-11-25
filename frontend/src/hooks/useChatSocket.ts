import { useCallback, useEffect, useRef, useState } from "react";
import { connectChatSocket, sendChatRequest } from "../services/chatSocket";
import type { ChatRequest, ChatResponse, ConnectionStatus } from "../types";

interface PendingRequest {
  conversationId: string;
  resolve: (response: ChatResponse) => void;
  reject: (error: Error) => void;
  onToken?: (token: string) => void;
  timeoutId: number;
}

const REQUEST_TIMEOUT_MS = 70_000;
const WS_AUTH_FAILED = 4401; // no valid session: log in again
const WS_ORIGIN_BLOCKED = 4403; // this website is not allowed by the backend
// Both must match backend/orchestrator/websocket.py

// The server refused the message because this user sent too many too fast.
// retryAfter is in seconds (backend sends it as "retry_after").
export class RateLimitError extends Error {
  retryAfter: number;

  constructor(message: string, retryAfter: number) {
    super(message);
    this.retryAfter = retryAfter;
  }
}

function asError(error: unknown): Error {
  return error instanceof Error ? error : new Error("The chat request failed.");
}

export function useChatSocket(onAuthError?: () => void): {
  status: ConnectionStatus;
  send: (
    request: ChatRequest,
    onToken?: (token: string) => void,
  ) => Promise<ChatResponse>;
  cancel: (conversationId: string) => void;
} {
  const [status, setStatus] = useState<ConnectionStatus>("connecting");
  const socketRef = useRef<WebSocket | null>(null);
  // One request in progress per conversation, so several chats can answer
  // at the same time. Replies are matched by conversation_id.
  const pendingRef = useRef<Map<string, PendingRequest>>(new Map());
  const onAuthErrorRef = useRef(onAuthError);

  useEffect(() => {
    onAuthErrorRef.current = onAuthError;
  }, [onAuthError]);

  const cancel = useCallback((conversationId: string) => {
    const socket = socketRef.current;

    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: "cancel", conversation_id: conversationId }));
    }
  }, []);

  // (Re)start the timer. It runs again on every token, so it only fires
  // if the server goes quiet for REQUEST_TIMEOUT_MS.
  const armTimeout = useCallback((pending: PendingRequest) => {
    window.clearTimeout(pending.timeoutId);

    pending.timeoutId = window.setTimeout(() => {
      if (pendingRef.current.get(pending.conversationId) !== pending) return;

      pendingRef.current.delete(pending.conversationId);
      pending.reject(new Error("The assistant took too long to reply. You can retry."));
      cancel(pending.conversationId);
    }, REQUEST_TIMEOUT_MS);
  }, [cancel]);

  const send = useCallback((request: ChatRequest, onToken?: (token: string) => void) => {
    return new Promise<ChatResponse>((resolve, reject) => {
      const socket = socketRef.current;
      const conversationId = request.conversation_id;

      if (!conversationId) {
        reject(new Error("No chat is selected."));
        return;
      }

      if (!socket || socket.readyState !== WebSocket.OPEN) {
        reject(new Error("Chat is not connected yet. Please try again shortly."));
        return;
      }

      if (pendingRef.current.has(conversationId)) {
        reject(new Error("A reply is already in progress in this chat."));
        return;
      }

      const pending: PendingRequest = { conversationId, resolve, reject, onToken, timeoutId: 0 };

      pendingRef.current.set(conversationId, pending);
      armTimeout(pending);

      try {
        sendChatRequest(socket, request);
      } catch (error) {
        window.clearTimeout(pending.timeoutId);
        pendingRef.current.delete(conversationId);
        reject(asError(error));
      }
    });
  }, [armTimeout]);

  useEffect(() => {
    let stopped = false;
    let retryTimer: number | undefined;
    let attempt = 0;

    const rejectAllPending = (message: string) => {
      for (const pending of pendingRef.current.values()) {
        window.clearTimeout(pending.timeoutId);
        pending.reject(new Error(message));
      }

      pendingRef.current.clear();
    };

    const openSocket = () => {
      if (stopped) return;

      setStatus(attempt === 0 ? "connecting" : "reconnecting");

      let socket: WebSocket;

      try {
        socket = connectChatSocket({
          onOpen: () => {
            if (stopped || socketRef.current !== socket) return;

            attempt = 0;
            setStatus("connected");
          },

          onData: (raw) => {
            if (stopped || socketRef.current !== socket) return;

            let pending: PendingRequest | undefined;

            try {
              const parsed: unknown = JSON.parse(String(raw));

              if (!parsed || typeof parsed !== "object") {
                throw new Error("The server returned an invalid reply.");
              }

              const value = parsed as Record<string, unknown>;

              // Which chat is this reply for?
              const conversationId =
                typeof value.conversation_id === "string" ? value.conversation_id : undefined;

              pending = conversationId
                ? pendingRef.current.get(conversationId)
                : pendingRef.current.size === 1
                  ? [...pendingRef.current.values()][0]
                  : undefined;

              if (!pending) return; // a late reply for a chat that is no longer waiting

              if (typeof value.error === "string") {
                if (typeof value.retry_after === "number") {
                  throw new RateLimitError(value.error, value.retry_after);
                }
                throw new Error(value.error);
              }

              // One piece of the answer: show it and keep waiting for more
              if (value.type === "token" && typeof value.content === "string") {
                pending.onToken?.(value.content);
                armTimeout(pending);
                return;
              }

              // Anything else is the final "done" message
              if (typeof value.answer !== "string") {
                throw new Error("The server reply did not include an answer.");
              }

              window.clearTimeout(pending.timeoutId);
              pendingRef.current.delete(pending.conversationId);

              pending.resolve({
                query: typeof value.query === "string" ? value.query : undefined,
                answer: value.answer,
                conversation_id: conversationId,
                results: Array.isArray(value.results) ? value.results : [],
              });
            } catch (error) {
              if (!pending) return;

              window.clearTimeout(pending.timeoutId);
              pendingRef.current.delete(pending.conversationId);
              pending.reject(asError(error));
            }
          },

          onClose: (event) => {
            if (stopped || socketRef.current !== socket) return;

            socketRef.current = null;
            rejectAllPending("The chat connection closed before a reply arrived. You can retry.");

            // No valid session (logged out or expired): retrying cannot work.
            if (event.code === WS_AUTH_FAILED) {
              stopped = true;
              setStatus("offline");
              onAuthErrorRef.current?.();
              return;
            }

            // The backend does not allow this website (check CORS_ORIGINS).
            if (event.code === WS_ORIGIN_BLOCKED) {
              stopped = true;
              setStatus("offline");
              return;
            }

            const delay = Math.min(1000 * Math.pow(2, attempt), 10_000);
            attempt += 1;

            setStatus(attempt >= 5 ? "offline" : "reconnecting");
            retryTimer = window.setTimeout(openSocket, delay);
          },

          onError: () => {
            if (stopped || socketRef.current !== socket) return;
            socket.close();
          },
        });

        socketRef.current = socket;
      } catch {
        socketRef.current = null;

        const delay = Math.min(1000 * Math.pow(2, attempt), 10_000);
        attempt += 1;

        setStatus(attempt >= 5 ? "offline" : "reconnecting");
        retryTimer = window.setTimeout(openSocket, delay);
      }
    };

    openSocket();

    return () => {
      stopped = true;

      if (retryTimer !== undefined) {
        window.clearTimeout(retryTimer);
      }

      const socket = socketRef.current;
      socketRef.current = null;

      rejectAllPending("The chat connection was closed.");

      if (socket) {
        if (socket.readyState === WebSocket.CONNECTING) {
          // Closing a socket that is still connecting makes the browser log
          // "WebSocket is closed before the connection is established".
          // (React StrictMode causes this in dev.) Close it as soon as it opens instead.
          socket.addEventListener(
            "open",
            () => socket.close(1000, "Component unmounted"),
            { once: true },
          );
        } else {
          socket.close(1000, "Component unmounted");
        }
      }
    };
  }, [armTimeout]);

  return { status, send, cancel };
}