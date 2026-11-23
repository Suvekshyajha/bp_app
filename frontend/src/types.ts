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
  user_message_id?: string;
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