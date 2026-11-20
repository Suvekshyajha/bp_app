import { useCallback, useEffect, useRef, useState } from "react";
import type { Conversation, ServerConversation } from "../types";
import { UnauthorizedError, getConversations } from "../api";
import { safeGetJson, safeSetJson } from "../services/storage";
import { getIdentity } from "../services/identity";

const DEFAULT_TITLE = "New chat";

export type ListStatus = "loading" | "ready" | "error";

function userId(): string {
  return getIdentity()?.userId ?? "default";
}

const selectedKey = () => "day2.selected." + userId();
// The server has no delete endpoint yet, so chats removed here are remembered
// as hidden in this browser.
const hiddenKey = () => "day2.hidden." + userId();

function newRecord(id: string): Conversation {
  const now = new Date().toISOString();
  return { id, title: DEFAULT_TITLE, createdAt: now, updatedAt: now, isLocal: true };
}

function fromServer(item: ServerConversation): Conversation {
  return {
    id: item.id,
    title: item.title?.trim() || DEFAULT_TITLE,
    createdAt: item.created_at,
    updatedAt: item.updated_at,
  };
}

function isDefaultTitle(title: string): boolean {
  return title === DEFAULT_TITLE || title === "My Chat";
}

export function useConversations(onAuthError?: () => void) {
  // Start on a blank local chat. The server list is merged in when it arrives.
  const [initial] = useState(() => newRecord(crypto.randomUUID()));
  const [conversations, setConversations] = useState<Conversation[]>([initial]);
  const [selectedId, setSelectedId] = useState<string>(initial.id);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [status, setStatus] = useState<ListStatus>("loading");
  const [loadingMore, setLoadingMore] = useState(false);

  const [hidden] = useState<Set<string>>(
    () => new Set(safeGetJson<string[]>(hiddenKey()) ?? []),
  );

  const onAuthErrorRef = useRef(onAuthError);

  useEffect(() => {
    onAuthErrorRef.current = onAuthError;
  }, [onAuthError]);

  const loadFirstPage = useCallback(() => {
    getConversations()
      .then((page) => {
        const server = page.items.map(fromServer).filter((c) => !hidden.has(c.id));

        // Keep blank local chats on top, then the server's list
        setConversations((current) => {
          const locals = current.filter((c) => c.isLocal);
          const known = new Set(locals.map((c) => c.id));
          return [...locals, ...server.filter((c) => !known.has(c.id))];
        });
        setNextCursor(page.next_cursor);

        // Reopen the chat that was open last time (only if still on the blank one)
        const saved = safeGetJson<string>(selectedKey());
        if (saved && server.some((c) => c.id === saved)) {
          setSelectedId((current) => (current === initial.id ? saved : current));
        }

        setStatus("ready");
      })
      .catch((error: unknown) => {
        if (error instanceof UnauthorizedError) onAuthErrorRef.current?.();
        setStatus("error");
      });
  }, [hidden, initial.id]);

  useEffect(() => {
    loadFirstPage();
  }, [loadFirstPage]);

  const retryLoad = useCallback(() => {
    setStatus("loading");
    loadFirstPage();
  }, [loadFirstPage]);

  // Next page of older chats ("Load more" in the sidebar)
  const loadMore = useCallback(async () => {
    if (!nextCursor || loadingMore) return;

    setLoadingMore(true);

    try {
      const page = await getConversations(nextCursor);
      const more = page.items.map(fromServer).filter((c) => !hidden.has(c.id));

      setConversations((current) => {
        const known = new Set(current.map((c) => c.id));
        return [...current, ...more.filter((c) => !known.has(c.id))];
      });
      setNextCursor(page.next_cursor);
    } catch (error) {
      if (error instanceof UnauthorizedError) onAuthErrorRef.current?.();
      // otherwise the button stays, so the user can try again
    } finally {
      setLoadingMore(false);
    }
  }, [hidden, loadingMore, nextCursor]);

  useEffect(() => {
    if (status === "ready") safeSetJson(selectedKey(), selectedId);
  }, [selectedId, status]);

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

  // The first message becomes the title; every message moves the chat to the top
  const touchConversation = useCallback((id: string, firstMessage?: string) => {
    const title = firstMessage?.replace(/\s+/g, " ").trim().slice(0, 40);

    setConversations((current) => {
      const target = current.find((c) => c.id === id);
      if (!target) return current;

      const updated: Conversation = {
        ...target,
        title: isDefaultTitle(target.title) && title ? title : target.title,
        updatedAt: new Date().toISOString(),
      };

      return [updated, ...current.filter((c) => c.id !== id)];
    });
  }, []);

  // The server has now stored a message for this chat
  const markSynced = useCallback((id: string) => {
    setConversations((current) =>
      current.map((c) => (c.id === id && c.isLocal ? { ...c, isLocal: false } : c)),
    );
  }, []);

  // Hides the chat in this browser only. The server keeps it (and the memory).
  const deleteConversation = useCallback(
    (id: string) => {
      hidden.add(id);
      safeSetJson(hiddenKey(), [...hidden]);

      const remaining = conversations.filter((c) => c.id !== id);

      if (remaining.length === 0) {
        const record = newRecord(crypto.randomUUID());

        setConversations([record]);
        setSelectedId(record.id);
        return;
      }

      setConversations(remaining);

      if (id === selectedId) setSelectedId(remaining[0].id);
    },
    [conversations, hidden, selectedId],
  );

  return {
    conversations,
    selectedId,
    status,
    hasMore: nextCursor !== null,
    loadingMore,
    loadMore,
    retryLoad,
    createConversation,
    selectConversation,
    touchConversation,
    markSynced,
    deleteConversation,
  };
}