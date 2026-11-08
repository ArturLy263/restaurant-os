import type { PendingOperationStore } from "../cashier/pendingOperations";

// Test-only reset of the shared store, kept out of the production class so
// that nothing in the app can wipe unresolved operations. It reaches the
// store's private state, so it checks that state's shape first and fails
// loudly (instead of silently resetting nothing) if the store changed.
interface StoreInternals {
  memory: Map<string, unknown>;
  removed: Record<string, unknown>;
  inFlight: Set<string>;
  lockFreeSince: Map<string, number>;
  unsaved: boolean;
  recoveredCorruptData: boolean;
}

const EXPECTED: Array<[keyof StoreInternals, (value: unknown) => boolean]> = [
  ["memory", (value) => value instanceof Map],
  ["removed", (value) => typeof value === "object" && value !== null],
  ["inFlight", (value) => value instanceof Set],
  ["lockFreeSince", (value) => value instanceof Map],
  ["unsaved", (value) => typeof value === "boolean"],
  ["recoveredCorruptData", (value) => typeof value === "boolean"],
];

/** Forgets a store's in-memory state, as a fresh page load would. */
export function resetStoreForTests(store: PendingOperationStore): void {
  const internals = store as unknown as StoreInternals;
  for (const [name, valid] of EXPECTED) {
    if (!valid(internals[name])) {
      throw new Error(`resetStoreForTests: PendingOperationStore.${name} changed; update this helper.`);
    }
  }
  internals.memory = new Map();
  internals.removed = {};
  internals.inFlight.clear();
  internals.lockFreeSince.clear();
  internals.unsaved = false;
  internals.recoveredCorruptData = false;
  store.reconcile();
}
