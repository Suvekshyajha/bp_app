
import { useState, type FormEvent } from "react";
import type { Identity } from "../types";
import { setIdentity } from "../services/identity";
import { API_BASE_URL } from "../config";

export function LoginPage({
  onAuthenticated,
}: {
  onAuthenticated: (identity: Identity) => void;
}) {
  const [isSignup, setIsSignup] = useState(true);
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError("");

    const name = displayName.trim();
    const enteredEmail = email.trim().toLowerCase();

    if (isSignup && !name) return;

    setLoading(true);

    try {
      const response = await fetch(
        `${API_BASE_URL}/api/${isSignup ? "signup" : "login"}`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Accept: "application/json",
          },
          body: JSON.stringify(
            isSignup
              ? { username: name, email: enteredEmail, password }
              : { email: enteredEmail, password },
          ),
        },
      );

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.detail || "Authentication failed.");
      }

      const identity: Identity = {
        userId: data.user_id,
        displayName: data.username,
        email: enteredEmail,
        token: data.access_token,
      };

      if (!identity.userId || !identity.token) {
        throw new Error("The server returned an invalid authentication response.");
      }

      const saved = setIdentity(identity);

      if (!saved) {
        throw new Error("Could not save your login. Check browser storage.");
      }

      onAuthenticated(identity);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Something went wrong. Please try again.",
      );
    } finally {
      setLoading(false);
    }
  };

  return (
    <main className="login-page">
      <section className="login-card" aria-labelledby="login-title">
        <div className="brand-mark brand-mark--large" aria-hidden="true">
          D
        </div>

        <p className="eyebrow">DAY2 BP CHAT</p>

        <h1 id="login-title">
          {isSignup
            ? "Create your account."
            : "Welcome back to your thinking space."}
        </h1>

        <p className="login-description">
          {isSignup
            ? "Create an account to start chatting and save your conversation history."
            : "Log in to continue your conversations."}
        </p>

        <form onSubmit={handleSubmit} className="login-form">
          {isSignup && (
            <>
              <label htmlFor="display-name">Your name</label>
              <input
                id="display-name"
                type="text"
                autoComplete="name"
                maxLength={48}
                value={displayName}
                onChange={(event) => setDisplayName(event.target.value)}
                placeholder="Your name"
                required
              />
            </>
          )}

          <label htmlFor="email">Email address</label>
          <input
            id="email"
            type="email"
            autoComplete="email"
            maxLength={254}
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            placeholder="you@example.com"
            required
          />

          <label htmlFor="password">Password</label>
          <input
            id="password"
            type="password"
            autoComplete={isSignup ? "new-password" : "current-password"}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            placeholder="Enter your password"
            minLength={8}
            required
          />

          {error && (
            <p className="error-message" role="alert">
              {error}
            </p>
          )}

          <button
            type="submit"
            className="primary-button"
            disabled={
              loading ||
              !email.trim() ||
              !password ||
              (isSignup && !displayName.trim())
            }
          >
            {loading
              ? "Please wait..."
              : isSignup
                ? "Create account"
                : "Log in"}{" "}
            <span aria-hidden="true">→</span>
          </button>
        </form>

        <p className="privacy-footnote">
          {isSignup ? "Already have an account? " : "Don't have an account? "}
          <button
            type="button"
            onClick={() => {
              setIsSignup(!isSignup);
              setError("");
            }}
          >
            {isSignup ? "Log in" : "Sign up"}
          </button>
        </p>
      </section>
    </main>
  );
}
