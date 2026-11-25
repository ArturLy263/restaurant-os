import { useCallback, useState, type ReactNode } from "react";

import type { CurrentUser } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { ForbiddenState } from "./CashierStates";
import { OrdersSection } from "./OrdersSection";
import { OverviewSection } from "./OverviewSection";
import { PaymentsSection } from "./PaymentsSection";
import { PendingOperationsBanner } from "./PendingOperationsBanner";
import { pendingOperations as defaultStore, type PendingOperationStore } from "./pendingOperations";
import { RegisterSection } from "./RegisterSection";
import { ReportsSection } from "./ReportsSection";
import { accessibleSections, canAccessSection, isSectionId, sectionById, type CashierSectionId } from "./sections";
import { SettingsSection } from "./SettingsSection";

const SECTION_KEY = "restaurantos.cashier.section";

interface CashierWorkspaceProps {
  token: string;
  user: CurrentUser;
  nav?: ReactNode;
  store?: PendingOperationStore;
}

export function CashierWorkspace({ token, user, nav, store = defaultStore }: CashierWorkspaceProps) {
  const { logout } = useAuth();
  const sections = accessibleSections(user);
  const [section, setSection] = useState<CashierSectionId>(() => {
    const stored = readStoredSection();
    return stored && canAccessSection(user, stored) ? stored : (sections[0]?.id ?? "settings");
  });

  // The bill open in Payments (chosen in Orders), and the restaurant's
  // currency once a bill told it (cashier orders carry no currency).
  const [billId, setBillId] = useState<string | null>(null);
  const [currency, setCurrency] = useState<string | null>(readStoredCurrency);

  function choose(next: CashierSectionId) {
    storeSection(next);
    setSection(next);
  }

  function openBill(id: string) {
    setBillId(id);
    if (canAccessSection(user, "payments")) choose("payments");
  }

  const rememberCurrency = useCallback((value: string) => {
    setCurrency(value);
    storeCurrency(value);
  }, []);

  return (
    <div className="app cx">
      <header className="topbar cx-topbar">
        <div className="topbar__brand">
          <span className="topbar__logo">RestaurantOS</span>
          <span className="cx-topbar__mode">Cashier</span>
          {nav}
        </div>
        <div className="topbar__actions">
          <span className="topbar__user" aria-label={`Signed in as ${user.name}${user.role ? `, ${user.role.name}` : ""}`}>
            {user.name}
            {user.role && <span className="tag">{user.role.name}</span>}
          </span>
          <button type="button" className="button button--ghost" onClick={() => logout()}>
            Sign out
          </button>
        </div>
      </header>

      <div className="cx-layout">
        <nav className="cx-nav" aria-label="Cashier sections">
          <ul className="cx-nav__list">
            {sections.map((candidate) => (
              <li key={candidate.id}>
                <button
                  type="button"
                  className={`cx-nav__button${candidate.id === section ? " cx-nav__button--current" : ""}`}
                  aria-current={candidate.id === section ? "page" : undefined}
                  onClick={() => choose(candidate.id)}
                >
                  {candidate.label}
                </button>
              </li>
            ))}
          </ul>
        </nav>

        <main className="cx-main">
          <PendingOperationsBanner store={store} user={user} token={token} />
          <SectionGuard user={user} section={section}>
            <SectionContent
              section={section}
              token={token}
              user={user}
              store={store}
              billId={billId}
              currency={currency}
              onOpenBill={openBill}
              onChooseOrder={() => choose("orders")}
              onCurrency={rememberCurrency}
            />
          </SectionGuard>
        </main>
      </div>
    </div>
  );
}

/** Renders a section only for users with its permissions. */
export function SectionGuard({
  user,
  section,
  children,
}: {
  user: CurrentUser;
  section: CashierSectionId;
  children: ReactNode;
}) {
  if (!canAccessSection(user, section)) return <ForbiddenState what={sectionById(section).label} />;
  return <>{children}</>;
}

interface SectionContentProps {
  section: CashierSectionId;
  token: string;
  user: CurrentUser;
  store: PendingOperationStore;
  billId: string | null;
  currency: string | null;
  onOpenBill(billId: string): void;
  onChooseOrder(): void;
  onCurrency(currency: string): void;
}

function SectionContent({ section, token, user, store, billId, currency, onOpenBill, onChooseOrder, onCurrency }: SectionContentProps) {
  switch (section) {
    case "overview":
      return <OverviewSection token={token} user={user} onCurrency={onCurrency} />;
    case "orders":
      return <OrdersSection token={token} user={user} store={store} currency={currency} onOpenBill={onOpenBill} />;
    case "payments":
      return (
        <PaymentsSection
          token={token}
          user={user}
          store={store}
          billId={billId}
          onChooseOrder={onChooseOrder}
          onCurrency={onCurrency}
        />
      );
    case "register":
      return <RegisterSection token={token} user={user} store={store} onCurrency={onCurrency} />;
    case "reports":
      return <ReportsSection token={token} user={user} store={store} />;
    case "settings":
      return <SettingsSection />;
  }
}

const CURRENCY_KEY = "restaurantos.cashier.currency";

function readStoredCurrency(): string | null {
  try {
    const value = window.sessionStorage.getItem(CURRENCY_KEY);
    return value && /^[A-Z]{3}$/.test(value) ? value : null;
  } catch {
    return null;
  }
}

function storeCurrency(value: string): void {
  try {
    window.sessionStorage.setItem(CURRENCY_KEY, value);
  } catch {
    // Not remembered across reloads.
  }
}

function readStoredSection(): CashierSectionId | null {
  try {
    const value = window.sessionStorage.getItem(SECTION_KEY);
    return isSectionId(value) ? value : null;
  } catch {
    return null;
  }
}

function storeSection(section: CashierSectionId): void {
  try {
    window.sessionStorage.setItem(SECTION_KEY, section);
  } catch {
    // Not remembered across reloads.
  }
}
