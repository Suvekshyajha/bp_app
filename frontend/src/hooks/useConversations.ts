import { useCallback, useEffect, useMemo, useState } from "react";
import type { Conversation } from "../types";
import { safeGetJson, safeRemove, safeSetJson } from "../services/storage";

const CONVERSATIONS_KEY = "day2.conversations";

function createConversationRecord(): Conversation {
  const now = new Date().toISOString();
  return { id: crypto.randomUUID(), title: "New chat", createdAt: now, updatedAt: now };
}

function readConversations(): Conversation[] {
  const saved = safeGetJson<Conversation[]>(CONVERSATIONS_KEY);
  if (!Array.isArray(saved)) return [createConversationRecord()];
  const valid = saved.filter((item) => item && typeof item.id === "string" && typeof item.title === "string");
  return valid.length > 0 ? valid : [createConversationRecord()];
}

export function useConversations() {
  const [initialState] = useState(() => {
    const initialConversations = readConversations();
    return {
      conversations: initialConversations,
      selectedId: initialConversations[0]?.id ?? "",
    };
  });
  const [conversations, setConversations] = useState<Conversation[]>(initialState.conversations);
  const [selectedId, setSelectedId] = useState<string>(initialState.selectedId);

  useEffect(() => {
    safeSetJson(CONVERSATIONS_KEY, conversations);
  }, [conversations]);

  const sortedConversations = useMemo(
    () => [...conversations].sort((a, b) => b.updatedAt.localeCompare(a.updatedAt)),
    [conversations],
  );

  const createConversation = useCallback(() => {
    const conversation = createConversationRecord();
    setConversations((current) => [conversation, ...current]);
    setSelectedId(conversation.id);
    return conversation.id;
  }, []);

  const selectConversation = useCallback((id: string) => {
    setSelectedId(id);
  }, []);

  const touchConversation = useCallback((id: string, firstMessage?: string) => {
    const title = firstMessage?.replace(/\s+/g, " ").trim().slice(0, 40);
    setConversations((current) => current.map((conversation) => {
      if (conversation.id !== id) return conversation;
      return {
        ...conversation,
        title: conversation.title === "New chat" && title ? title : conversation.title,
        updatedAt: new Date().toISOString(),
      };
    }));
  }, []);

  const deleteConversation = useCallback((id: string) => {
    safeRemove("day2.messages." + id);
    const remaining = conversations.filter((conversation) => conversation.id !== id);
    if (remaining.length === 0) {
      const next = createConversationRecord();
      setConversations([next]);
      setSelectedId(next.id);
      return;
    }
    setConversations(remaining);
    if (selectedId === id) {
      const next = [...remaining].sort((a, b) => b.updatedAt.localeCompare(a.updatedAt))[0];
      setSelectedId(next.id);
    }
  }, [conversations, selectedId]);

  return {
    conversations: sortedConversations,
    selectedId,
    createConversation,
    selectConversation,
    touchConversation,
    deleteConversation,
  };
}
