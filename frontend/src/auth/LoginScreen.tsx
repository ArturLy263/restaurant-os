import { useState, type FormEvent } from "react";

import { isApiError } from "../api/client";
import { useAuth } from "./AuthContext";

export function LoginScreen({ notice }: { notice: string | null }) {
  const { login } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await login(email.trim(), password);
    } catch (caught) {
      setError(loginErrorMessage(caught));
      setSubmitting(false);
    }
  }

  return (
    <main className="login">
      <form className="login__panel" onSubmit={handleSubmit} noValidate>
        <h1 className="login__title">RestaurantOS</h1>
        <p className="login__subtitle">Kitchen display</p>

        {notice && !error && (
          <p className="banner banner--info" role="status">
            {notice}
          </p>
        )}
        {error && (
          <p className="banner banner--error" role="alert">
            {error}
          </p>
        )}

        <label className="field">
          <span className="field__label">Email</span>
          <input
            className="field__input"
            type="email"
            name="email"
            autoComplete="username"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
        </label>

        <label className="field">
          <span className="field__label">Password</span>
          <input
            className="field__input"
            type="password"
            name="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>

        <button
          className="button button--primary button--block"
          type="submit"
          disabled={submitting || !email || !password}
        >
          {submitting ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}

function loginErrorMessage(error: unknown): string {
  if (!isApiError(error)) return "Sign in failed. Please try again.";
  switch (error.code) {
    case "INVALID_CREDENTIALS":
      return "Invalid email or password.";
    case "USER_INACTIVE":
      return "This account is inactive. Ask a manager to reactivate it.";
    case "VALIDATION_ERROR":
      return "Enter a valid email address and password.";
    default:
      return error.message;
  }
}
