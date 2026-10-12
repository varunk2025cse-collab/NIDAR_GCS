import { useState } from "react";
import { ApiError, api } from "../api/client";
import type { Operator } from "../api/types";

/**
 * Operator sign-in.
 *
 * The demo accounts are listed because this is a competition build and a judge
 * needs to get in. Remove the block below (`DEMO_ACCOUNTS`) before any field
 * deployment -- in the field, accounts come from `scripts/bootstrap.py` and
 * credentials are not printed on the sign-in page.
 */

const DEMO_ACCOUNTS: { username: string; password: string; role: string; can: string }[] = [
  {
    username: "commander",
    password: "Nidar@2026!",
    role: "ADMIN",
    can: "Everything, including the audit log and operator management",
  },
  {
    username: "operator",
    password: "Nidar@2026!",
    role: "OPERATOR",
    can: "Fly missions, confirm survivors, dispatch deliveries",
  },
  {
    username: "observer",
    password: "Nidar@2026!",
    role: "VIEWER",
    can: "Read-only. Cannot command an aircraft",
  },
];

export function Login({ onAuthenticated }: { onAuthenticated: (operator: Operator) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const signIn = async (user: string, pass: string) => {
    setBusy(true);
    setError(null);
    try {
      const token = await api.login(user, pass);
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
      <aside className="login__hero">
        <div className="login__brand">
          <img className="login__mark" src="/favicon.svg" alt="" />
          <div>
            <div className="login__wm">
              NIDAR <em>RESCUESWARM</em>
            </div>
            <div className="login__tag">Ground Control Station</div>
          </div>
        </div>

        <div className="login__pitch">
          <h1 className="login__h">
            Three aircraft. One operator.
            <br />
            <span>Nobody left unaccounted for.</span>
          </h1>
          <p className="login__p">
            Two scouts search a partitioned area while a delivery aircraft carries aid to
            confirmed survivors. PX4 flies the aircraft and owns every failsafe; this station
            plans the mission, watches what actually happens, and tells you the truth about it —
            including when it does not know.
          </p>
        </div>

        <div className="login__facts">
          <div>
            <div className="login__fn">3</div>
            <div className="login__fl">Aircraft</div>
          </div>
          <div>
            <div className="login__fn">MAVLink</div>
            <div className="login__fl">Real PX4 link</div>
          </div>
          <div>
            <div className="login__fn">PostGIS</div>
            <div className="login__fl">Spatial truth</div>
          </div>
          <div>
            <div className="login__fn">0</div>
            <div className="login__fl">Simulated values</div>
          </div>
        </div>
      </aside>

      <main className="login__panel">
        <form
          className="login__card"
          onSubmit={(event) => {
            event.preventDefault();
            void signIn(username, password);
          }}
        >
          <h2 className="login__title">Operator sign-in</h2>
          <p className="login__sub">Authorised personnel only. Every command is audited.</p>

          {error ? <div className="login__err">{error}</div> : null}

          <label className="login__f">
            <span className="login__l">Operator</span>
            <input
              className="input"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              autoComplete="username"
              autoFocus
              required
            />
          </label>

          <label className="login__f">
            <span className="login__l">Password</span>
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

          <div className="login__demo">
            <div className="login__dt">Demo accounts — click to sign in</div>
            <div className="roles">
              {DEMO_ACCOUNTS.map((account) => (
                <button
                  key={account.username}
                  type="button"
                  className="role"
                  disabled={busy}
                  title={account.can}
                  onClick={() => {
                    setUsername(account.username);
                    setPassword(account.password);
                    void signIn(account.username, account.password);
                  }}
                >
                  <span className="role__n">{account.username}</span>
                  <span
                    className={`badge badge--${
                      account.role === "ADMIN"
                        ? "info"
                        : account.role === "OPERATOR"
                          ? "ok"
                          : "neutral"
                    }`}
                  >
                    {account.role}
                  </span>
                </button>
              ))}
            </div>
            <p className="note" style={{ marginTop: 9 }}>
              All three use <code>Nidar@2026!</code>. Remove this block before field use; create
              real accounts with <code>python -m scripts.bootstrap</code>.
            </p>
          </div>
        </form>
      </main>
    </div>
  );
}
