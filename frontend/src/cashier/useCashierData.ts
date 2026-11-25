import { useCallback, useEffect, useRef, useState } from "react";

import { isApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";

export type DataState<T> =
  | { status: "loading" }
  | { status: "ready"; data: T; refreshing: boolean; refreshError: Error | null }
  | { status: "error"; error: Error }
  | { status: "forbidden"; error: Error };

/**
 * Loads `load` when `key` changes and on reload(). A failed reload keeps
 * the last data (marked stale); a 401 ends the session, a 403 is reported
 * as forbidden. `key` null loads nothing.
 */
export function useCashierData<T>(
  key: string | null,
  load: (signal: AbortSignal) => Promise<T>,
): { state: DataState<T>; reload(): void } {
  const { handleAuthError } = useAuth();
  const [state, setState] = useState<DataState<T>>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);
  const loadRef = useRef(load);
  loadRef.current = load;
  const lastKey = useRef<string | null>(null);

  useEffect(() => {
    if (key === null) return;
    const controller = new AbortController();
    const sameKey = lastKey.current === key;
    lastKey.current = key;
    setState((previous) =>
      sameKey && previous.status === "ready" ? { ...previous, refreshing: true } : { status: "loading" },
    );
    loadRef
      .current(controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setState({ status: "ready", data, refreshing: false, refreshError: null });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (handleAuthError(error)) return;
        const failure = error instanceof Error ? error : new Error(String(error));
        if (isApiError(error) && error.isForbidden) {
          setState({ status: "forbidden", error: failure });
          return;
        }
        setState((previous) =>
          previous.status === "ready" && sameKey
            ? { ...previous, refreshing: false, refreshError: failure }
            : { status: "error", error: failure },
        );
      });
    return () => controller.abort();
  }, [key, attempt, handleAuthError]);

  const reload = useCallback(() => setAttempt((count) => count + 1), []);
  return { state, reload };
}
