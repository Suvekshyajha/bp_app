
import type { Identity } from "../types";
import { safeGetJson, safeRemove, safeSetJson } from "./storage";

const IDENTITY_KEY = "day2.identity";

function isIdentity(value: unknown): value is Identity {
  if (!value || typeof value !== "object") return false;

  const candidate = value as Record<string, unknown>;

  return (
    typeof candidate.userId === "string" &&
    candidate.userId.trim().length > 0 &&
    typeof candidate.displayName === "string" &&
    candidate.displayName.trim().length > 0 &&
    typeof candidate.email === "string" &&
    candidate.email.trim().length > 0 &&
    typeof candidate.token === "string" &&
    candidate.token.trim().length > 0
  );
}

export function getIdentity(): Identity | null {
  const value = safeGetJson<unknown>(IDENTITY_KEY);
  return isIdentity(value) ? value : null;
}

export function getUserId(): string | null {
  return getIdentity()?.userId ?? null;
}

export function getDisplayName(): string | null {
  return getIdentity()?.displayName ?? null;
}

export function getEmail(): string | null {
  return getIdentity()?.email ?? null;
}

export function getToken(): string | null {
  return getIdentity()?.token ?? null;
}

export function setIdentity(identity: Identity): boolean {
  return safeSetJson(IDENTITY_KEY, identity);
}

export function clearIdentity(): boolean {
  return safeRemove(IDENTITY_KEY);
}
