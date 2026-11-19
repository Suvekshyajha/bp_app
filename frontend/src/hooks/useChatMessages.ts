import { useCallback, useEffect, useRef, useState } from "react";
import type { ChatMessage } from "../types";
import { safeGetJson, safeRemove, safeSetJson } from "../services/storage";
import { getIdentity } from "../services/identity";

function messagesKey(conversationId: string): string {
  const userId = getIdentity()?.userId ?? "default";

  // The first chat keeps the old key, so existing history is not lost
  return conversationId === userId
    ? "day2.messages." + userId
    : "day2.messages." + userId + "." + conversationId;
}

function readMessages(conversationId: string): ChatMessage[] {
  const saved = safeGetJson<ChatMessage[]>(messagesKey(conversationId));
  return Array.isArray(saved) ? saved : [];
}

const NO_MESSAGES: ChatMessage[] = [];

// Keeps the messages of every chat that has been opened, so a chat that is
// still answering in the background keeps filling in while another is open.
export function useChatMessages(selectedId: string) {
  const [byChat, setByChat] = useState<Record<string, ChatMessage[]>>(() => ({
    [selectedId]: readMessages(selectedId),
  }));

  const dirtyRef = useRef<Set<string>>(new Set());
  const deletedRef = useRef<Set<string>>(new Set());

  // Opening a chat for the first time: load it from storage
  if (!(selectedId in byChat)) {
    setByChat((current) => ({ ...current, [selectedId]: readMessages(selectedId) }));
  }

  // Save only the chats that changed
  useEffect(() => {
    for (const id of dirtyRef.current) {
      if (byChat[id]) safeSetJson(messagesKey(id), byChat[id]);
    }

    dirtyRef.current.clear();
  }, [byChat]);

  const change = useCallback(
    (id: string, update: (messages: ChatMessage[]) => ChatMessage[]) => {
      if (deletedRef.current.has(id)) return; // a reply for a deleted chat

      dirtyRef.current.add(id);

      setByChat((current) => ({
        ...current,
        [id]: update(current[id] ?? readMessages(id)),
      }));
    },
    [],
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

  const removeChat = useCallback((id: string) => {
    deletedRef.current.add(id);
    dirtyRef.current.delete(id);
    safeRemove(messagesKey(id));

    setByChat((current) => {
      const next = { ...current };
      delete next[id];
      return next;
    });
  }, []);

  return {
    messages: byChat[selectedId] ?? NO_MESSAGES,
    addMessage,
    updateMessage,
    removeMessage,
    removeChat,
  };
}