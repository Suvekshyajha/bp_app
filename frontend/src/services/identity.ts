import type { Identity } from "../types";
import { safeGetJson, safeRemove, safeSetJson } from "./storage";

const IDENTITY_KEY = "day2.identity";

export function getIdentity(): Identity | null {
  const value = safeGetJson<Identity>(IDENTITY_KEY);
  if (!value || typeof value.userId !== "string" || typeof value.displayName !== "string") {
    return null;
  }
  return value;
}

export function getUserId(): string | null {
  return getIdentity()?.userId ?? null;
}

export function getDisplayName(): string | null {
  return getIdentity()?.displayName ?? null;
}

export function setIdentity(identity: Identity): boolean {
  return safeSetJson(IDENTITY_KEY, identity);
}

export function clearIdentity(): boolean {
  return safeRemove(IDENTITY_KEY);
}
