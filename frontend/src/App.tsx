import { useEffect, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import type { Identity } from "./types";
import {
  clearIdentity,
  getIdentity,
  setIdentity as saveIdentity,
} from "./services/identity";
import { UnauthorizedError, getMe, logoutRequest } from "./api";
import { LoginPage } from "./pages/LoginPage";
import { ChatPage } from "./pages/ChatPage";

export default function App() {
  const [identity, setIdentity] = useState<Identity | null>(getIdentity);

  // If we think we are logged in, ask the server to confirm the session.
  const [checking, setChecking] = useState(() => getIdentity() !== null);

  useEffect(() => {
    if (!checking) return;

    let active = true;

    getMe()
      .then((me) => {
        if (!active) return;

        const confirmed: Identity = {
          userId: me.user_id,
          displayName: me.username,
          email: me.email,
        };

        saveIdentity(confirmed);
        setIdentity(confirmed);
      })
      .catch((error: unknown) => {
        // Session ended or expired: go to the login page.
        // Other errors (API down) keep the screen and show the API banner.
        if (error instanceof UnauthorizedError) {
          clearIdentity();
          if (active) setIdentity(null);
        }
      })
      .finally(() => {
        if (active) setChecking(false);
      });

    return () => {
      active = false;
    };
    // Runs once when the app opens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const logOut = () => {
    clearIdentity();
    setIdentity(null);
    // Also end the session on the server (best effort)
    void logoutRequest().catch(() => undefined);
  };

  if (checking) {
    return (
      <main className="login-page">
        <p>Loading…</p>
      </main>
    );
  }

  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={identity ? <Navigate to="/chat" replace /> : <LoginPage onAuthenticated={setIdentity} />} />
        <Route path="/chat" element={identity ? <ChatPage identity={identity} onLogout={logOut} /> : <Navigate to="/login" replace />} />
        <Route path="*" element={<Navigate to={identity ? "/chat" : "/login"} replace />} />
      </Routes>
    </BrowserRouter>
  );
}