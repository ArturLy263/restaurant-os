import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { isApiError } from "../api/client";
import { getMe, login as loginRequest } from "../api/endpoints";
import type { CurrentUser } from "../api/types";
import { clearToken, readToken, tokenExpiresAt, writeToken } from "./tokenStorage";

export const SESSION_EXPIRED_NOTICE =
  "Your session has expired. Please sign in again.";

type AuthState =
  | { status: "restoring" }
  | { status: "anonymous"; notice: string | null }
  | { status: "authenticated"; token: string; user: CurrentUser };

interface AuthContextValue {
  state: AuthState;
  login(email: string, password: string): Promise<void>;
  logout(notice?: string | null): void;
  /** Ends the session on a 401; returns whether the error was handled. */
  handleAuthError(error: unknown): boolean;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>(() =>
    readToken() ? { status: "restoring" } : { status: "anonymous", notice: null },
  );

  const logout = useCallback((notice: string | null = null) => {
    clearToken();
    setState({ status: "anonymous", notice });
  }, []);

  // Restore a stored session: the token is only trusted once /me accepts it.
  useEffect(() => {
    if (state.status !== "restoring") return;
    const token = readToken();
    if (!token) {
      setState({ status: "anonymous", notice: null });
      return;
    }
    const controller = new AbortController();
    getMe(token, controller.signal).then(
      (user) => setState({ status: "authenticated", token, user }),
      (error: unknown) => {
        if (controller.signal.aborted) return;
        clearToken();
        setState({
          status: "anonymous",
          notice:
            isApiError(error) && error.isUnauthorized
              ? SESSION_EXPIRED_NOTICE
              : isApiError(error)
                ? error.message
                : null,
        });
      },
    );
    return () => controller.abort();
  }, [state.status]);

  // Sign out when the token expires rather than on the next failing call.
  const token = state.status === "authenticated" ? state.token : null;
  useEffect(() => {
    if (!token) return;
    const expiresAt = tokenExpiresAt(token);
    if (expiresAt === null) return;
    const delay = expiresAt - Date.now();
    // setTimeout delays above ~24.8 days overflow; such tokens are not issued.
    if (delay > 2_147_483_647) return;
    const timer = window.setTimeout(
      () => logout(SESSION_EXPIRED_NOTICE),
      Math.max(0, delay),
    );
    return () => window.clearTimeout(timer);
  }, [token, logout]);

  const login = useCallback(async (email: string, password: string) => {
    const { access_token: newToken } = await loginRequest(email, password);
    const user = await getMe(newToken);
    writeToken(newToken);
    setState({ status: "authenticated", token: newToken, user });
  }, []);

  const handleAuthError = useCallback(
    (error: unknown) => {
      if (isApiError(error) && error.isUnauthorized) {
        logout(SESSION_EXPIRED_NOTICE);
        return true;
      }
      return false;
    },
    [logout],
  );

  const value = useMemo(
    () => ({ state, login, logout, handleAuthError }),
    [state, login, logout, handleAuthError],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
