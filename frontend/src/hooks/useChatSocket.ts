
import { useCallback, useEffect, useRef, useState } from "react";
import { connectChatSocket, sendChatRequest } from "../services/chatSocket";
import type { ChatRequest, ChatResponse, ConnectionStatus } from "../types";

interface PendingRequest {
  resolve: (response: ChatResponse) => void;
  reject: (error: Error) => void;
  timeoutId: number;
}

const REQUEST_TIMEOUT_MS = 70_000;

function asError(error: unknown): Error {
  return error instanceof Error ? error : new Error("The chat request failed.");
}

export function useChatSocket(token: string): {
  status: ConnectionStatus;
  send: (request: ChatRequest) => Promise<ChatResponse>;
} {
  const [status, setStatus] = useState<ConnectionStatus>("connecting");
  const socketRef = useRef<WebSocket | null>(null);
  const pendingRef = useRef<PendingRequest | null>(null);

  const send = useCallback((request: ChatRequest) => {
    return new Promise<ChatResponse>((resolve, reject) => {
      const socket = socketRef.current;

      if (!socket || socket.readyState !== WebSocket.OPEN) {
        reject(new Error("Chat is not connected yet. Please try again shortly."));
        return;
      }

      if (pendingRef.current) {
        reject(new Error("A reply is already in progress."));
        return;
      }

      const timeoutId = window.setTimeout(() => {
        const pending = pendingRef.current;
        if (!pending) return;

        pendingRef.current = null;
        pending.reject(new Error("The assistant took too long to reply. You can retry."));
        socketRef.current?.close(4000, "Request timed out");
      }, REQUEST_TIMEOUT_MS);

      pendingRef.current = { resolve, reject, timeoutId };

      try {
        sendChatRequest(socket, request);
      } catch (error) {
        window.clearTimeout(timeoutId);
        pendingRef.current = null;
        reject(asError(error));
      }
    });
  }, []);

  useEffect(() => {
    if (!token) {
      setStatus("offline");
      return;
    }

    let stopped = false;
    let retryTimer: number | undefined;
    let attempt = 0;

    const rejectPending = (message: string) => {
      const pending = pendingRef.current;
      if (!pending) return;

      window.clearTimeout(pending.timeoutId);
      pendingRef.current = null;
      pending.reject(new Error(message));
    };

    const openSocket = () => {
      if (stopped) return;

      setStatus(attempt === 0 ? "connecting" : "reconnecting");

      let socket: WebSocket;

      try {
        socket = connectChatSocket(token, {
          onOpen: () => {
            if (stopped || socketRef.current !== socket) return;

            attempt = 0;
            setStatus("connected");
          },

          onData: (raw) => {
            if (stopped || socketRef.current !== socket) return;

            const pending = pendingRef.current;
            if (!pending) return;

            window.clearTimeout(pending.timeoutId);
            pendingRef.current = null;

            try {
              const parsed: unknown = JSON.parse(String(raw));

              if (!parsed || typeof parsed !== "object") {
                throw new Error("The server returned an invalid reply.");
              }

              const value = parsed as Record<string, unknown>;

              if (typeof value.error === "string") {
                throw new Error(value.error);
              }

              if (typeof value.answer !== "string") {
                throw new Error("The server reply did not include an answer.");
              }

              pending.resolve({
                query: typeof value.query === "string" ? value.query : undefined,
                answer: value.answer,
                conversation_id:
                  typeof value.conversation_id === "string"
                    ? value.conversation_id
                    : undefined,
                results: Array.isArray(value.results) ? value.results : [],
              });
            } catch (error) {
              pending.reject(asError(error));
            }
          },

          onClose: () => {
            if (stopped || socketRef.current !== socket) return;

            socketRef.current = null;
            rejectPending("The chat connection closed before a reply arrived. You can retry.");

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

      rejectPending("The chat connection was closed.");
      socket?.close(1000, "Component unmounted");
    };
  }, [token]);

  return { status, send };
}
