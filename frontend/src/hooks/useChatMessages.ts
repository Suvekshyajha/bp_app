import { useCallback, useEffect, useMemo, useState } from "react";
import type { ChatMessage } from "../types";
import { safeGetJson, safeSetJson } from "../services/storage";

function readMessages(conversationId: string): ChatMessage[] {
  const saved = safeGetJson<ChatMessage[]>("day2.messages." + conversationId);
  return Array.isArray(saved) ? saved : [];
}

export function useChatMessages(conversationId: string) {
  const [messagesByConversation, setMessagesByConversation] = useState<Record<string, ChatMessage[]>>({});
  const messages = useMemo(
    () => messagesByConversation[conversationId] ?? readMessages(conversationId),
    [conversationId, messagesByConversation],
  );

  useEffect(() => {
    const current = messagesByConversation[conversationId];
    if (current) safeSetJson("day2.messages." + conversationId, current);
  }, [conversationId, messagesByConversation]);

  const addMessage = useCallback((message: ChatMessage) => {
    setMessagesByConversation((current) => ({
      ...current,
      [conversationId]: [...(current[conversationId] ?? readMessages(conversationId)), message],
    }));
  }, [conversationId]);

  const updateMessage = useCallback((id: string, update: (message: ChatMessage) => ChatMessage) => {
    setMessagesByConversation((current) => {
      const existing = current[conversationId] ?? readMessages(conversationId);
      return {
        ...current,
        [conversationId]: existing.map((message) => message.id === id ? update(message) : message),
      };
    });
  }, [conversationId]);

  return { messages, addMessage, updateMessage };
}
