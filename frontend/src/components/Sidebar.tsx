import type { Conversation } from "../types";
import type { ListStatus } from "../hooks/useConversations";
import { ChatIcon, InfoIcon, LogoutIcon, PlusIcon, TrashIcon } from "./Icons";

export function Sidebar({
  conversations,
  selectedId,
  displayName,
  locked,
  status,
  hasMore,
  loadingMore,
  onNew,
  onSelect,
  onDelete,
  onLoadMore,
  onReload,
  onLogout,
}: {
  conversations: Conversation[];
  selectedId: string;
  displayName: string;
  locked: boolean;
  status: ListStatus;
  hasMore: boolean;
  loadingMore: boolean;
  onNew: () => void;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onLoadMore: () => void;
  onReload: () => void;
  onLogout: () => void;
}) {
  return (
    <aside className="sidebar" aria-label="Conversations">
      <div className="sidebar-brand"><div className="brand-mark">I</div><span className="brand-name">Info<strong>Mate</strong></span></div>
      <button type="button" className="new-chat-button" onClick={onNew} disabled={locked}>
        <PlusIcon /> New chat
      </button>
      <div className="conversation-heading">YOUR CHATS <span>{conversations.length}{hasMore ? "+" : ""}</span></div>
      <nav className="conversation-nav" aria-label="Your conversations">
        {conversations.map((conversation) => (
          <div className={"conversation-item " + (conversation.id === selectedId ? "is-active" : "")} key={conversation.id}>
            <button
              type="button"
              className="conversation-select"
              aria-current={conversation.id === selectedId ? "page" : undefined}
              onClick={() => onSelect(conversation.id)}
              disabled={locked}
            >
              <span className="conversation-icon" aria-hidden="true"><ChatIcon /></span>
              <span className="conversation-title">{conversation.title}</span>
            </button>
            <button
              type="button"
              className="delete-chat-button"
              aria-label={"Delete " + conversation.title}
              title="Delete conversation"
              onClick={() => onDelete(conversation.id)}
              disabled={locked}
            ><TrashIcon /></button>
          </div>
        ))}
        {status === "loading" && <p className="list-note">Loading chats…</p>}
        {status === "error" && (
          <p className="list-note">
            Could not load your chats.{" "}
            <button type="button" className="load-more-button" onClick={onReload}>Retry</button>
          </p>
        )}
        {hasMore && (
          <button type="button" className="load-more-button" onClick={onLoadMore} disabled={loadingMore || locked}>
            {loadingMore ? "Loading…" : "Load more chats"}
          </button>
        )}
      </nav>
      <div className="sidebar-bottom">
        <div className="demo-note"><InfoIcon /><p>Your chats are saved on the server and load as you scroll.</p></div>
        <div className="account-row">
          <div className="account-avatar" aria-hidden="true">{displayName.slice(0, 1).toUpperCase()}</div>
          <div className="account-copy"><strong>{displayName}</strong><span>Local demo account</span></div>
          <button type="button" className="logout-button" onClick={onLogout} disabled={locked} aria-label="Log out" title="Log out"><LogoutIcon /></button>
        </div>
      </div>
    </aside>
  );
}