export type MessageRole = "user" | "assistant";
export type MessageStatus = "pending" | "sent" | "failed";
export type ConnectionStatus = "connecting" | "connected" | "reconnecting" | "offline";

export interface RetrievedPayload {
  message_id?: string;
  user_id?: string;
  conversation_id?: string;
  role?: string;
  content?: string;
  timestamp?: string;
  // Only on an excerpt from an uploaded document (a past chat message has none)
  document_id?: string;
  filename?: string;
  chunk_index?: number;
  section_title?: string;
  page?: number;
}

export interface RetrievedMessage {
  id?: string;
  score?: number;
  payload?: RetrievedPayload;
}

export interface ChatMessage {
  id: string;
  role: MessageRole;
  content: string;
  createdAt: string;
  status?: MessageStatus;
  context?: RetrievedMessage[];
  error?: string;
}

export interface Conversation {
  id: string;
  title: string;
  createdAt: string;
  updatedAt: string;
  // Created in this browser, but the server has not seen a message for it yet
  isLocal?: boolean;
}

// ---- Paginated REST responses (backend/orchestrator/history.py) ----
export interface Page<T> {
  items: T[];
  next_cursor: string | null; // null on the last page
}

export interface ServerConversation {
  id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
}

export interface ServerMessage {
  id: string;
  role: MessageRole;
  content: string;
  created_at: string;
}

export interface ChatRequest {
  query: string;
  conversation_id?: string;
  user_message_id?: string; // the idempotency key
  regenerate?: boolean; // true = a new answer to the same question
  document_ids?: string[]; // documents to search besides the chat (max 5)
}

export interface ChatResponse {
  query?: string;
  answer: string;
  conversation_id?: string;
  results: RetrievedMessage[];
}

export interface Identity {
  userId: string;
  displayName: string;
  email: string;
}

// ---- Documents (backend/orchestrator/documents.py) ----
export type DocumentStatus =
  | "pending"
  | "processing"
  | "ready"
  | "failed"
  | "deleting";

// What the server sends (snake_case)
export interface ServerDocument {
  id: string;
  filename: string;
  status: DocumentStatus;
  error?: string | null; // a short safe code, never a stack trace
  chunk_count?: number | null;
  created_at?: string | null;
}

// What the UI uses
export interface DocumentInfo {
  id: string;
  filename: string;
  status: DocumentStatus;
  error?: string;
  chunkCount?: number;
  createdAt: string;
}
