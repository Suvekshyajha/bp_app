
import { useCallback, useState } from "react";
import type { Conversation } from "../types";
import { safeGetJson, safeSetJson } from "../services/storage";
import { getIdentity } from "../services/identity";

function createConversationRecord(): Conversation {
  const now = new Date().toISOString();

  return {
    id: getIdentity()?.userId ?? "default",
    title: "My Chat",
    createdAt: now,
    updatedAt: now,
  };
}

export function useConversations() {
  const [conversation] = useState<Conversation>(() => {
    const userId = getIdentity()?.userId ?? "default";
    const saved = safeGetJson<Conversation>("day2.conversation." + userId);

    return saved ?? createConversationRecord();
  });

  const [updatedConversation, setUpdatedConversation] =
    useState<Conversation>(conversation);

  const saveConversation = useCallback((updated: Conversation) => {
    setUpdatedConversation(updated);
    const userId = getIdentity()?.userId ?? "default";
    safeSetJson("day2.conversation." + userId, updated);
  }, []);

  const touchConversation = useCallback(
    (_id: string, firstMessage?: string) => {
      const title =
        firstMessage?.replace(/\s+/g, " ").trim().slice(0, 40);

      saveConversation({
        ...updatedConversation,
        title:
          updatedConversation.title === "My Chat" && title
            ? title
            : updatedConversation.title,
        updatedAt: new Date().toISOString(),
      });
    },
    [saveConversation, updatedConversation],
  );

  const selectConversation = useCallback((_id: string) => {}, []);

  const createConversation = useCallback(() => {
    return updatedConversation.id;
  }, [updatedConversation.id]);

  const deleteConversation = useCallback((_id: string) => {
    // The backend owns the persistent conversation.
    // Do not delete it from the browser.
  }, []);

  return {
    conversations: [updatedConversation],
    selectedId: updatedConversation.id,
    createConversation,
    selectConversation,
    touchConversation,
    deleteConversation,
  };
}
