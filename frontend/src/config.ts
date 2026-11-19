// By default the API is on the same host as this page (port 8000).
// This matters for the login cookie: "localhost" and "127.0.0.1" count as
// different sites, so the page and the API must use the same one.
const configuredApiBase =
  import.meta.env.VITE_API_BASE_URL ||
  `${window.location.protocol}//${window.location.hostname}:8000`;

export const API_BASE_URL = configuredApiBase.replace(/\/+$/, "");

const derivedSocketUrl =
  API_BASE_URL.replace(/^http:/, "ws:").replace(/^https:/, "wss:") +
  "/ws/chat";

export const CHAT_SOCKET_URL =
  import.meta.env.VITE_WS_URL || derivedSocketUrl;