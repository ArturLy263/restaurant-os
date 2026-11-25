import { useCallback, useRef, useState } from "react";

import type { MutationRequest } from "../api/cashier";
import { isApiError } from "../api/client";
import type { CurrentUser } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { executeOperation, OperationBlockedError, type PendingOperationStore } from "./pendingOperations";

// Every cashier write goes through pending-operation storage: stored with
// its Idempotency-Key and body BEFORE it is sent, then sent once. The
// screens never send a mutation themselves.

export type OperationOutcome<Result> =
  | { ok: true; result: Result }
  // refused: the server said no before anything changed (4xx); the error
  //   is shown where the action was taken and the record is removed.
  // unknown: the server may have done it; the record stays in the
  //   unresolved-operations banner, where only a retry with the same key
  //   (never a new attempt) is offered.
  // blocked: the store would not send it (e.g. already being sent).
  // local: it could not even be stored (e.g. it looked like card data).
  // signed_out: the session ended (401); the sign-in screen takes over.
  | { ok: false; kind: "refused" | "unknown" | "blocked" | "local" | "signed_out"; error: unknown };

const UNKNOWN_OUTCOME_TEXT =
  "The server did not confirm this. It is kept under “Unresolved operations” above: retry it there, " +
  "do not enter it again.";

export interface CashierOperationRunner {
  /** Sends one operation; a second call while one is running is ignored (null). */
  run<Result>(request: MutationRequest<Result>): Promise<OperationOutcome<Result> | null>;
  busy: boolean;
}

export function useCashierOperation(store: PendingOperationStore, user: CurrentUser, token: string): CashierOperationRunner {
  const { handleAuthError } = useAuth();
  const running = useRef(false);
  const [busy, setBusy] = useState(false);

  const run = useCallback(
    async <Result>(request: MutationRequest<Result>): Promise<OperationOutcome<Result> | null> => {
      // A synchronous guard: a double click must not create two operations
      // (two keys would mean two payments).
      if (running.current) return null;
      running.current = true;
      setBusy(true);
      try {
        let operationId: string;
        try {
          operationId = (await store.create(user, request)).id;
        } catch (error) {
          return { ok: false, kind: "local", error };
        }
        try {
          const result = await executeOperation<Result>(store, operationId, { user, token });
          return { ok: true, result };
        } catch (error) {
          if (handleAuthError(error)) return { ok: false, kind: "signed_out", error };
          if (error instanceof OperationBlockedError) return { ok: false, kind: "blocked", error };
          const op = store.get(operationId);
          if (op && store.effectiveStatus(op) === "failed" && store.canDismiss(op, user)) {
            // Nothing was committed; the error is shown at the form.
            await store.dismiss(operationId, user).catch(() => undefined);
            return { ok: false, kind: "refused", error };
          }
          return { ok: false, kind: "unknown", error };
        }
      } finally {
        running.current = false;
        setBusy(false);
      }
    },
    [store, user, token, handleAuthError],
  );

  return { run, busy };
}

/** What to tell the user about a failed operation; null when nothing (signed out). */
export function failureText(outcome: Extract<OperationOutcome<unknown>, { ok: false }>): string | null {
  if (outcome.kind === "signed_out") return null;
  if (outcome.kind === "unknown") return UNKNOWN_OUTCOME_TEXT;
  const error = outcome.error;
  if (isApiError(error)) return error.requestId ? `${error.message} (request ${error.requestId})` : error.message;
  return error instanceof Error ? error.message : String(error);
}

/** The backend error code of a refused operation, if any. */
export function refusalCode(outcome: OperationOutcome<unknown> | null): string | null {
  if (!outcome || outcome.ok || outcome.kind !== "refused" || !isApiError(outcome.error)) return null;
  return outcome.error.code;
}
