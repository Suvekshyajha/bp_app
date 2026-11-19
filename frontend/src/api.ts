import { API_BASE_URL } from "./config";

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
  const response = await fetch(API_BASE_URL + "/api/me", {
    credentials: "include",
    headers: { Accept: "application/json" },
  });
  if (response.status === 401) throw new UnauthorizedError();
  if (!response.ok) throw new Error("HTTP " + response.status);
  return (await response.json()) as MeResponse;
}

// Ends the session on the server and clears the cookie
export async function logoutRequest(): Promise<void> {
  await fetch(API_BASE_URL + "/api/logout", {
    method: "POST",
    credentials: "include",
  });
}