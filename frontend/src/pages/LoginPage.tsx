import { useState, type FormEvent } from "react";
import type { Identity } from "../types";
import { setIdentity } from "../services/identity";

export function LoginPage({ onAuthenticated }: { onAuthenticated: (identity: Identity) => void }) {
  const [displayName, setDisplayName] = useState("");

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const name = displayName.trim().slice(0, 48);
    if (!name) return;
    const identity = { userId: crypto.randomUUID(), displayName: name };
    setIdentity(identity);
    onAuthenticated(identity);
  };

  return (
    <main className="login-page">
      <section className="login-card" aria-labelledby="login-title">
        <div className="brand-mark brand-mark--large" aria-hidden="true">D</div>
        <p className="eyebrow">DAY2 BP CHAT</p>
        <h1 id="login-title">Welcome back to your thinking space.</h1>
        <p className="login-description">Start with a name. You can explore the chat without creating a real account.</p>
        <div className="demo-login-note"><strong>Demo mode: no real login.</strong> Your demo identity and chat history live in this browser only.</div>
        <form onSubmit={handleSubmit} className="login-form">
          <label htmlFor="display-name">What should we call you?</label>
          <input
            id="display-name"
            autoComplete="name"
            maxLength={48}
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
            placeholder="Your name"
            required
          />
          <button type="submit" className="primary-button" disabled={!displayName.trim()}>Continue <span aria-hidden="true">→</span></button>
        </form>
        <p className="privacy-footnote">No passwords or account details are collected.</p>
      </section>
    </main>
  );
}
