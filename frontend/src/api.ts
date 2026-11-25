import { API_BASE_URL } from "./config";
import type {
  DocumentInfo,
  Page,
  ServerConversation,
  ServerDocument,
  ServerMessage,
} from "./types";

export interface HealthResponse {
  status: string;
}

export interface MeResponse {
  user_id: string;
  username: string;
  email: string;
}

// Thrown when the server says we are not logged in (HTTP 401)
export class UnauthorizedError extends Error {
  constructor() {
    super("Not logged in.");
  }
}

// Thrown when a chat does not exist for this user (HTTP 404)
export class NotFoundError extends Error {
  constructor() {
    super("Not found.");
  }
}

// Shared GET helper for the cookie-protected endpoints
async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(API_BASE_URL + path, {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (response.status === 401) throw new UnauthorizedError();
  if (response.status === 404) throw new NotFoundError();
  if (!response.ok) throw new Error("HTTP " + response.status);
  return (await response.json()) as T;
}

function pageQuery(cursor?: string | null, limit?: number): string {
  const params = new URLSearchParams();
  if (limit) params.set("limit", String(limit));
  if (cursor) params.set("cursor", cursor);
  const text = params.toString();
  return text ? "?" + text : "";
}

export async function getHealth(): Promise<HealthResponse> {
  const response = await fetch(API_BASE_URL + "/api/health", {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error("Health check returned HTTP " + response.status);
  }
  return (await response.json()) as HealthResponse;
}

// Asks the server who is logged in (uses the session cookie)
export async function getMe(): Promise<MeResponse> {
  return getJson<MeResponse>("/api/me");
}

// One page of the user's chats, most recently active first
export function getConversations(
  cursor?: string | null,
  limit?: number,
): Promise<Page<ServerConversation>> {
  return getJson("/api/conversations" + pageQuery(cursor, limit));
}

// One page of a chat's messages. No cursor = the latest page.
// Each page is oldest-first; next_cursor leads to OLDER messages.
export function getMessages(
  conversationId: string,
  cursor?: string | null,
  limit?: number,
): Promise<Page<ServerMessage>> {
  return getJson(
    "/api/conversations/" +
      encodeURIComponent(conversationId) +
      "/messages" +
      pageQuery(cursor, limit),
  );
}

// Ends the session on the server and clears the cookie
export async function logoutRequest(): Promise<void> {
  await fetch(API_BASE_URL + "/api/logout", {
    method: "POST",
    credentials: "include",
  });
}

// ---------- Documents ----------

// The server explains a refusal in {"detail": "..."}. Show that, not "HTTP 413".
async function failureMessage(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    const detail = (body as { detail?: unknown } | null)?.detail;
    if (typeof detail === "string" && detail) return detail;
  } catch {
    // not JSON: fall through
  }
  return "HTTP " + response.status;
}

// Same 401 / 404 handling as getJson, for the calls that are not a plain GET
async function sendRequest(path: string, init: RequestInit): Promise<Response> {
  const response = await fetch(API_BASE_URL + path, {
    ...init,
    credentials: "include",
  });
  if (response.status === 401) throw new UnauthorizedError();
  if (response.status === 404) throw new NotFoundError();
  if (!response.ok) throw new Error(await failureMessage(response));
  return response;
}

export function toDocumentInfo(item: ServerDocument): DocumentInfo {
  return {
    id: item.id,
    filename: item.filename,
    status: item.status,
    error: item.error || undefined,
    chunkCount: item.chunk_count ?? undefined,
    createdAt: item.created_at || new Date().toISOString(),
  };
}

// Sends one file. No Content-Type header on purpose: the browser must add the
// multipart boundary itself. Same file again gives back the existing document.
export async function uploadDocument(file: File): Promise<DocumentInfo> {
  const form = new FormData();
  form.append("file", file);

  const response = await sendRequest("/api/documents", {
    method: "POST",
    body: form,
  });
  return toDocumentInfo((await response.json()) as ServerDocument);
}

export async function getDocuments(): Promise<DocumentInfo[]> {
  const items = await getJson<ServerDocument[]>("/api/documents");
  return items.map(toDocumentInfo);
}

export async function getDocument(id: string): Promise<DocumentInfo> {
  return toDocumentInfo(
    await getJson<ServerDocument>("/api/documents/" + encodeURIComponent(id)),
  );
}

export async function deleteDocument(id: string): Promise<void> {
  await sendRequest("/api/documents/" + encodeURIComponent(id), {
    method: "DELETE",
  });
}
