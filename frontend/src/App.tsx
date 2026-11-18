import { useState } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import type { Identity } from "./types";
import { clearIdentity, getIdentity } from "./services/identity";
import { LoginPage } from "./pages/LoginPage";
import { ChatPage } from "./pages/ChatPage";

export default function App() {
  const [identity, setIdentity] = useState<Identity | null>(getIdentity);

  const logOut = () => {
    clearIdentity();
    setIdentity(null);
  };

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
