import { useState } from "react";

import { Permission, type CurrentUser } from "./api/types";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import { LoginScreen } from "./auth/LoginScreen";
import { KdsScreen } from "./kds/KdsScreen";
import { PosScreen } from "./pos/PosScreen";

type Mode = "pos" | "kds";

const MODE_KEY = "restaurantos.mode";
const MODE_LABELS: Record<Mode, string> = { pos: "Waiter POS", kds: "Kitchen display" };

/** The screens the user's permissions allow, the POS first. */
export function availableModes(user: CurrentUser): Mode[] {
  const permissions = new Set(user.permissions);
  const modes: Mode[] = [];
  if (permissions.has(Permission.OrdersView) && permissions.has(Permission.OrdersCreate)) {
    modes.push("pos");
  }
  if (permissions.has(Permission.PreparationsView)) modes.push("kds");
  return modes;
}

function Root() {
  const { state } = useAuth();

  switch (state.status) {
    case "restoring":
      return (
        <section className="state state--full" aria-busy="true">
          <div className="spinner" aria-hidden="true" />
          <p className="state__text">Restoring session…</p>
        </section>
      );
    case "anonymous":
      return <LoginScreen notice={state.notice} />;
    case "authenticated":
      return <Workspace key={state.user.id} token={state.token} user={state.user} />;
  }
}

function Workspace({ token, user }: { token: string; user: CurrentUser }) {
  const { logout } = useAuth();
  const modes = availableModes(user);
  const [mode, setMode] = useState<Mode | null>(() => {
    const stored = readStoredMode();
    return stored && modes.includes(stored) ? stored : (modes[0] ?? null);
  });

  function choose(next: Mode) {
    storeMode(next);
    setMode(next);
  }

  const nav =
    modes.length > 1 ? (
      <nav className="modes" aria-label="Screens">
        {modes.map((candidate) => (
          <button
            key={candidate}
            type="button"
            className={`modes__button${candidate === mode ? " modes__button--current" : ""}`}
            aria-current={candidate === mode ? "page" : undefined}
            onClick={() => choose(candidate)}
          >
            {MODE_LABELS[candidate]}
          </button>
        ))}
      </nav>
    ) : null;

  if (mode === "pos") return <PosScreen token={token} user={user} nav={nav} />;
  if (mode === "kds") return <KdsScreen token={token} user={user} nav={nav} />;

  return (
    <div className="app">
      <header className="topbar">
        <span className="topbar__logo">RestaurantOS</span>
        <button type="button" className="button button--ghost" onClick={() => logout()}>
          Sign out
        </button>
      </header>
      <section className="state">
        <h2 className="state__title">No screen for your role yet</h2>
        <p className="state__text">
          {user.role ? `The ${user.role.name} role` : "Your account"} has no access to the waiter POS or the
          kitchen display.
        </p>
      </section>
    </div>
  );
}

function readStoredMode(): Mode | null {
  try {
    const value = window.sessionStorage.getItem(MODE_KEY);
    return value === "pos" || value === "kds" ? value : null;
  } catch {
    return null;
  }
}

function storeMode(mode: Mode): void {
  try {
    window.sessionStorage.setItem(MODE_KEY, mode);
  } catch {
    // Not remembered across reloads.
  }
}

export function App() {
  return (
    <AuthProvider>
      <Root />
    </AuthProvider>
  );
}
