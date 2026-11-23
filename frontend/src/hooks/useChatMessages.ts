import { useCallback, useEffect, useRef, useState } from "react";
import type { ChatMessage, ServerMessage } from "../types";
import { UnauthorizedError, getMessages } from "../api";

export type HistoryStatus = "loading" | "ready" | "error";

interface ChatState {
  messages: ChatMessage[];
  nextCursor: string | null; // leads to OLDER messages; null = nothing older
  status: HistoryStatus;
  loadingEarlier: boolean;
}

const EMPTY_CHAT: ChatState = {
  messages: [],
  nextCursor: null,
  status: "ready",
  loadingEarlier: false,
};

const NO_MESSAGES: ChatMessage[] = [];

function fromServer(item: ServerMessage): ChatMessage {
  return {
    id: item.id,
    role: item.role,
    content: item.content,
    createdAt: item.created_at,
    status: item.role === "user" ? "sent" : undefined,
  };
}

// Put a page of older messages in front of what is already shown
function addOlder(current: ChatMessage[], older: ServerMessage[]): ChatMessage[] {
  const known = new Set(current.map((message) => message.id));

  return [
    ...older.map(fromServer).filter((message) => !known.has(message.id)),
    ...current,
  ];
}

// The server is the source of truth. A chat's latest page loads when it is
// first opened; older pages load on demand. Chats that are still answering in
// the background keep filling in while another chat is open.
//
// isLocal = a brand-new chat the server has never seen: nothing to load.
export function useChatMessages(
  selectedId: string,
  isLocal: boolean,
  onAuthError?: () => void,
) {
  const [chats, setChats] = useState<Record<string, ChatState>>({});

  const requestedRef = useRef<Set<string>>(new Set());
  const deletedRef = useRef<Set<string>>(new Set());
  const onAuthErrorRef = useRef(onAuthError);

  useEffect(() => {
    onAuthErrorRef.current = onAuthError;
  }, [onAuthError]);

  const patch = useCallback(
    (id: string, update: (chat: ChatState) => ChatState) => {
      setChats((current) => ({ ...current, [id]: update(current[id] ?? EMPTY_CHAT) }));
    },
    [],
  );

  const loadLatest = useCallback(
    (id: string) => {
      getMessages(id)
        .then((page) => {
          if (deletedRef.current.has(id)) return;

          patch(id, (chat) => ({
            ...chat,
            status: "ready",
            nextCursor: page.next_cursor,
            messages: addOlder(chat.messages, page.items),
          }));
        })
        .catch((error: unknown) => {
          if (deletedRef.current.has(id)) return;
          if (error instanceof UnauthorizedError) onAuthErrorRef.current?.();

          patch(id, (chat) => ({ ...chat, status: "error" }));
        });
    },
    [patch],
  );

  // First time a chat is opened: load its latest page
  useEffect(() => {
    if (requestedRef.current.has(selectedId)) return;

    requestedRef.current.add(selectedId);

    if (!isLocal) loadLatest(selectedId);
  }, [isLocal, loadLatest, selectedId]);

  const retryLoad = useCallback(() => {
    patch(selectedId, (chat) => ({ ...chat, status: "loading" }));
    loadLatest(selectedId);
  }, [loadLatest, patch, selectedId]);

  // "Load earlier messages"
  const loadEarlier = useCallback(async () => {
    const id = selectedId;
    const chat = chats[id];

    if (!chat?.nextCursor || chat.loadingEarlier) return;

    patch(id, (current) => ({ ...current, loadingEarlier: true }));

    try {
      const page = await getMessages(id, chat.nextCursor);

      if (deletedRef.current.has(id)) return;

      patch(id, (current) => ({
        ...current,
        loadingEarlier: false,
        nextCursor: page.next_cursor,
        messages: addOlder(current.messages, page.items),
      }));
    } catch (error) {
      if (error instanceof UnauthorizedError) onAuthErrorRef.current?.();

      patch(id, (current) => ({ ...current, loadingEarlier: false }));
    }
  }, [chats, patch, selectedId]);

  const change = useCallback(
    (id: string, update: (messages: ChatMessage[]) => ChatMessage[]) => {
      if (deletedRef.current.has(id)) return; // a reply for a deleted chat

      patch(id, (chat) => ({ ...chat, messages: update(chat.messages) }));
    },
    [patch],
  );

  const addMessage = useCallback(
    (id: string, message: ChatMessage) => {
      change(id, (messages) => [...messages, message]);
    },
    [change],
  );

  const updateMessage = useCallback(
    (id: string, messageId: string, update: (message: ChatMessage) => ChatMessage) => {
      change(id, (messages) =>
        messages.map((message) => (message.id === messageId ? update(message) : message)),
      );
    },
    [change],
  );

  const removeMessage = useCallback(
    (id: string, messageId: string) => {
      change(id, (messages) => messages.filter((message) => message.id !== messageId));
    },
    [change],
  );

  // Keep the messages up to and including `messageId`, drop everything after it
  const removeAfter = useCallback(
    (id: string, messageId: string) => {
      change(id, (messages) => {
        const index = messages.findIndex((message) => message.id === messageId);

        return index === -1 ? messages : messages.slice(0, index + 1);
      });
    },
    [change],
  );

  const removeChat = useCallback((id: string) => {
    deletedRef.current.add(id);

    setChats((current) => {
      const next = { ...current };
      delete next[id];
      return next;
    });
  }, []);

  const chat = chats[selectedId];

  return {
    messages: chat?.messages ?? NO_MESSAGES,
    historyStatus: chat?.status ?? (isLocal ? "ready" : "loading"),
    hasEarlier: chat?.nextCursor != null,
    loadingEarlier: chat?.loadingEarlier ?? false,
    loadEarlier,
    retryLoad,
    addMessage,
    updateMessage,
    removeMessage,
    removeAfter, // NEW: this line was missing
    removeChat,
  } as const;
}