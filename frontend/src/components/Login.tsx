import { useState } from "react";
import { ApiError, api } from "../api/client";
import type { Operator } from "../api/types";

export function Login({ onAuthenticated }: { onAuthenticated: (operator: Operator) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const token = await api.login(username, password);
      onAuthenticated(token.operator);
    } catch (cause) {
      if (cause instanceof ApiError) {
        setError(
          cause.status === 0
            ? "Cannot reach the GCS backend. Is it running on this ground station?"
            : cause.status === 401
              ? "Incorrect username or password."
              : `${cause.code}: ${cause.message}`,
        );
      } else {
        setError("Sign-in failed.");
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login">
      <form className="login__card" onSubmit={submit}>
        <h1 className="login__title">NIDAR RescueSwarm</h1>
        <p className="login__sub">Ground Control Station — operator sign-in</p>

        {error ? <div className="login__error">{error}</div> : null}

        <label className="login__field">
          <span className="login__label">Operator</span>
          <input
            className="input"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
            autoFocus
            required
          />
        </label>

        <label className="login__field">
          <span className="login__label">Password</span>
          <input
            className="input"
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
            required
          />
        </label>

        <button type="submit" className="btn btn--primary login__btn" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>

        <p className="note" style={{ marginTop: 14 }}>
          Create the first operator on the ground station with
          <br />
          <code>python -m scripts.bootstrap --username admin</code>
        </p>
      </form>
    </div>
  );
}
