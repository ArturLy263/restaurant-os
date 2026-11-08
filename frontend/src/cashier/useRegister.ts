import { useCallback, useEffect, useState } from "react";

import { getRegister } from "../api/cashier";
import type { CashRegister } from "../api/cashierTypes";
import { isApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { usePageVisible } from "../lib/usePageVisible";

export const REGISTER_POLL_MS = 15_000;

export type RegisterState =
  | { status: "loading" }
  | { status: "ready"; register: CashRegister; updatedAt: Date; refreshError: Error | null }
  | { status: "error"; error: Error }
  | { status: "forbidden"; error: Error };

/**
 * GET /cash/register now and every 15 s while the page is visible and the
 * component is mounted (the Overview is shown). Polling pauses while the
 * tab is hidden and refreshes at once when it is shown again; it stops on
 * 403 (the role lost access) until retried.
 */
export function useRegister(token: string): { state: RegisterState; retry(): void } {
  const { handleAuthError } = useAuth();
  const visible = usePageVisible();
  const [state, setState] = useState<RegisterState>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);
  const forbidden = state.status === "forbidden";

  useEffect(() => {
    if (!visible || forbidden) return;
    let cancelled = false;
    let timer: number | undefined;
    let controller: AbortController | undefined;

    async function load() {
      controller = new AbortController();
      try {
        const register = await getRegister(token, controller.signal);
        if (cancelled) return;
        setState({ status: "ready", register, updatedAt: new Date(), refreshError: null });
      } catch (error) {
        if (cancelled) return;
        if (handleAuthError(error)) return;
        const failure = error instanceof Error ? error : new Error(String(error));
        if (isApiError(error) && error.isForbidden) {
          setState({ status: "forbidden", error: failure });
          return;
        }
        // Keep showing the last data, marked as stale.
        setState((previous) =>
          previous.status === "ready" ? { ...previous, refreshError: failure } : { status: "error", error: failure },
        );
      }
      if (!cancelled) timer = window.setTimeout(load, REGISTER_POLL_MS);
    }

    void load();
    return () => {
      cancelled = true;
      controller?.abort();
      window.clearTimeout(timer);
    };
  }, [token, visible, forbidden, attempt, handleAuthError]);

  const retry = useCallback(() => {
    setState((previous) => (previous.status === "ready" ? previous : { status: "loading" }));
    setAttempt((count) => count + 1);
  }, []);

  return { state, retry };
}
