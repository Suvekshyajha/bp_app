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
}

export interface ChatRequest {
  query: string;
  conversation_id?: string;
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