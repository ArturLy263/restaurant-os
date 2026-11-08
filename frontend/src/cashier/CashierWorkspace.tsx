import { useState, type ReactNode } from "react";

import type { CurrentUser } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { ForbiddenState, PlaceholderSection } from "./CashierStates";
import { OverviewSection } from "./OverviewSection";
import { PendingOperationsBanner } from "./PendingOperationsBanner";
import { pendingOperations as defaultStore, type PendingOperationStore } from "./pendingOperations";
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

  function choose(next: CashierSectionId) {
    storeSection(next);
    setSection(next);
  }

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
            <SectionContent section={section} token={token} user={user} />
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

function SectionContent({ section, token, user }: { section: CashierSectionId; token: string; user: CurrentUser }) {
  switch (section) {
    case "overview":
      return <OverviewSection token={token} user={user} />;
    case "orders":
      return (
        <PlaceholderSection title="Orders">
          Orders waiting for payment and settled orders will be listed here.
        </PlaceholderSection>
      );
    case "payments":
      return (
        <PlaceholderSection title="Payments">
          Taking cash and card payments, splitting bills and refunds will be available here.
        </PlaceholderSection>
      );
    case "register":
      return (
        <PlaceholderSection title="Cash Register">
          Opening, handing over and closing cash shifts, and cash in / out, will be available here.
        </PlaceholderSection>
      );
    case "reports":
      return (
        <PlaceholderSection title="Reports">
          Business day and shift reports will be available here.
        </PlaceholderSection>
      );
    case "settings":
      return <SettingsSection />;
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
