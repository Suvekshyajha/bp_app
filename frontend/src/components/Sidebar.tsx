import type { Conversation } from "../types";

export function Sidebar({
  conversations,
  selectedId,
  displayName,
  locked,
  onNew,
  onSelect,
  onDelete,
  onLogout,
}: {
  conversations: Conversation[];
  selectedId: string;
  displayName: string;
  locked: boolean;
  onNew: () => void;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onLogout: () => void;
}) {
  return (
    <aside className="sidebar" aria-label="Conversations">
      <div className="sidebar-brand"><div className="brand-mark">I</div><span>Info<strong>mate</strong></span></div>
      <button type="button" className="new-chat-button" onClick={onNew} disabled={locked}>
        <span aria-hidden="true">＋</span> New chat
      </button>
      <div className="conversation-heading">YOUR CHATS <span>{conversations.length}</span></div>
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
              <span className="conversation-icon" aria-hidden="true">◷</span>
              <span className="conversation-title">{conversation.title}</span>
            </button>
            <button
              type="button"
              className="delete-chat-button"
              aria-label={"Delete " + conversation.title}
              title="Delete conversation"
              onClick={() => onDelete(conversation.id)}
              disabled={locked}
            >×</button>
          </div>
        ))}
      </nav>
      <div className="sidebar-bottom">
        <div className="demo-note"><span aria-hidden="true">ⓘ</span><p>Demo mode. Chats are saved only in this browser.</p></div>
        <div className="account-row">
          <div className="account-avatar" aria-hidden="true">{displayName.slice(0, 1).toUpperCase()}</div>
          <div className="account-copy"><strong>{displayName}</strong><span>Local demo account</span></div>
          <button type="button" className="logout-button" onClick={onLogout} disabled={locked} aria-label="Log out" title="Log out">↗</button>
        </div>
      </div>
    </aside>
  );
}
