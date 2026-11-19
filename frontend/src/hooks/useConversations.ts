import { useCallback, useEffect, useState } from "react";
import type { Conversation } from "../types";
import { safeGetJson, safeSetJson } from "../services/storage";
import { getIdentity } from "../services/identity";

const DEFAULT_TITLE = "New chat";

function userId(): string {
  return getIdentity()?.userId ?? "default";
}

const listKey = () => "day2.conversations." + userId();
const selectedKey = () => "day2.selected." + userId();

function newRecord(id: string): Conversation {
  const now = new Date().toISOString();
  return { id, title: DEFAULT_TITLE, createdAt: now, updatedAt: now };
}

function isDefaultTitle(title: string): boolean {
  return title === DEFAULT_TITLE || title === "My Chat";
}

function loadConversations(): Conversation[] {
  const saved = safeGetJson<Conversation[]>(listKey());

  if (Array.isArray(saved) && saved.length > 0) return saved;

  // First start after this upgrade: keep the one old chat (its id is the user id)
  const legacy = safeGetJson<Conversation>("day2.conversation." + userId());

  return [legacy ?? newRecord(userId())];
}

export function useConversations() {
  const [conversations, setConversations] = useState<Conversation[]>(loadConversations);

  const [selectedId, setSelectedId] = useState<string>(() => {
    const list = loadConversations();
    const saved = safeGetJson<string>(selectedKey());

    return list.some((conversation) => conversation.id === saved)
      ? (saved as string)
      : list[0].id;
  });

  useEffect(() => {
    safeSetJson(listKey(), conversations);
  }, [conversations]);

  useEffect(() => {
    safeSetJson(selectedKey(), selectedId);
  }, [selectedId]);

  // Start a new chat (newest first) and open it
  const createConversation = useCallback((): string => {
    const record = newRecord(crypto.randomUUID());

    setConversations((current) => [record, ...current]);
    setSelectedId(record.id);

    return record.id;
  }, []);

  const selectConversation = useCallback((id: string) => {
    setSelectedId(id);
  }, []);

  // The first message becomes the title; every message bumps the time
  const touchConversation = useCallback((id: string, firstMessage?: string) => {
    const title = firstMessage?.replace(/\s+/g, " ").trim().slice(0, 40);

    setConversations((current) =>
      current.map((conversation) =>
        conversation.id !== id
          ? conversation
          : {
              ...conversation,
              title:
                isDefaultTitle(conversation.title) && title
                  ? title
                  : conversation.title,
              updatedAt: new Date().toISOString(),
            },
      ),
    );
  }, []);

  // Removes the chat from this browser only. The server keeps the memory.
  const deleteConversation = useCallback(
    (id: string) => {
      const remaining = conversations.filter((conversation) => conversation.id !== id);

      if (remaining.length === 0) {
        const record = newRecord(crypto.randomUUID());

        setConversations([record]);
        setSelectedId(record.id);
        return;
      }

      setConversations(remaining);

      if (id === selectedId) setSelectedId(remaining[0].id);
    },
    [conversations, selectedId],
  );

  return {
    conversations,
    selectedId,
    createConversation,
    selectConversation,
    touchConversation,
    deleteConversation,
  };
}