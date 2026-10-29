// The access token lives in sessionStorage: it survives a reload of the
// display but not closing the tab, and is never sent anywhere except in the
// Authorization header. Storage may be unavailable (private mode, blocked
// site data), in which case the session simply lasts as long as the page.
const TOKEN_KEY = "restaurantos.accessToken";

export function readToken(): string | null {
  try {
    return window.sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function writeToken(token: string): void {
  try {
    window.sessionStorage.setItem(TOKEN_KEY, token);
  } catch {
    // Not persisted; the in-memory session still works.
  }
}

export function clearToken(): void {
  try {
    window.sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    // Nothing stored.
  }
}

/**
 * The token's `exp` in milliseconds, read without verifying the signature
 * (only the backend can do that); used to end the session on time instead
 * of waiting for the next 401. null if the token has no readable `exp`.
 */
export function tokenExpiresAt(token: string): number | null {
  const payload = token.split(".")[1];
  if (!payload) return null;
  try {
    const base64 = payload.replace(/-/g, "+").replace(/_/g, "/");
    const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
    const claims = JSON.parse(atob(padded)) as { exp?: unknown };
    return typeof claims.exp === "number" ? claims.exp * 1000 : null;
  } catch {
    return null;
  }
}
