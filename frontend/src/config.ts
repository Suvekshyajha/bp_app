const configuredApiBase =
  import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

export const API_BASE_URL = configuredApiBase.replace(/\/+$/, "");

const derivedSocketUrl =
  API_BASE_URL.replace(/^http:/, "ws:").replace(/^https:/, "wss:") +
  "/ws/chat";

export const CHAT_SOCKET_URL =
  import.meta.env.VITE_WS_URL || derivedSocketUrl;