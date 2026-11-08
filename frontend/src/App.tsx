import { useEffect, useState } from "react";

import { Permission, type CurrentUser } from "./api/types";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import { LoginScreen } from "./auth/LoginScreen";
import { CashierWorkspace } from "./cashier/CashierWorkspace";
import { pendingOperations } from "./cashier/pendingOperations";
import { canUseCashierWorkspace } from "./cashier/sections";
import { KdsScreen } from "./kds/KdsScreen";
import { PosScreen } from "./pos/PosScreen";
import { setCashierWorkspace } from "./theme/theme";

type Mode = "pos" | "kds" | "cashier";

const MODE_KEY = "restaurantos.mode";
const MODE_LABELS: Record<Mode, string> = { pos: "Waiter POS", kds: "Kitchen display", cashier: "Cashier" };

/** The screens the user's permissions allow, the POS first, the cashier workspace last. */
export function availableModes(user: CurrentUser): Mode[] {
  const permissions = new Set(user.permissions);
  const modes: Mode[] = [];
  if (permissions.has(Permission.OrdersView) && permissions.has(Permission.OrdersCreate)) {
    modes.push("pos");
  }
  if (permissions.has(Permission.PreparationsView)) modes.push("kds");
  if (canUseCashierWorkspace(user)) modes.push("cashier");
  return modes;
}

function Root() {
  const { state } = useAuth();

  // The cashier theme belongs to the cashier workspace only (while a
  // session is restored it stays as index.html set it, to avoid a flash).
  const anonymous = state.status === "anonymous";
  useEffect(() => {
    if (anonymous) setCashierWorkspace(false);
  }, [anonymous]);

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

  useEffect(() => {
    setCashierWorkspace(mode === "cashier");
  }, [mode]);

  // Pending-operation upkeep (save unsaved records, persist expiry proven by
  // server time) while someone is signed in, on every screen; stops at
  // sign-out because this component unmounts.
  useEffect(() => pendingOperations.startUpkeep(), []);

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
  if (mode === "cashier") return <CashierWorkspace token={token} user={user} nav={nav} />;

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
          {user.role ? `The ${user.role.name} role` : "Your account"} has no access to the waiter POS, the
          kitchen display or the cashier workspace.
        </p>
      </section>
    </div>
  );
}

function readStoredMode(): Mode | null {
  try {
    const value = window.sessionStorage.getItem(MODE_KEY);
    return value === "pos" || value === "kds" || value === "cashier" ? value : null;
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
