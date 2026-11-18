
import { useCallback, useEffect, useMemo, useState } from "react";
import type { ChatMessage } from "../types";
import { safeGetJson, safeSetJson } from "../services/storage";
import { getIdentity } from "../services/identity";

function getMessagesKey(): string {
  const userId = getIdentity()?.userId ?? "default";
  return "day2.messages." + userId;
}

function readMessages(): ChatMessage[] {
  const saved = safeGetJson<ChatMessage[]>(getMessagesKey());
  return Array.isArray(saved) ? saved : [];
}

export function useChatMessages(_conversationId: string) {
  const [storedMessages, setStoredMessages] =
    useState<ChatMessage[]>(readMessages);

  const messages = useMemo(
    () => storedMessages,
    [storedMessages],
  );

  useEffect(() => {
    safeSetJson(getMessagesKey(), storedMessages);
  }, [storedMessages]);

  const addMessage = useCallback((message: ChatMessage) => {
    setStoredMessages((current) => [...current, message]);
  }, []);

  const updateMessage = useCallback(
    (id: string, update: (message: ChatMessage) => ChatMessage) => {
      setStoredMessages((current) =>
        current.map((message) =>
          message.id === id ? update(message) : message,
        ),
      );
    },
    [],
  );

  return { messages, addMessage, updateMessage };
}
