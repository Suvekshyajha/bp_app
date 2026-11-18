import { API_BASE_URL } from "./config";

export interface HealthResponse {
  status: string;
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
