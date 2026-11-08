import type { LockManagerLike } from "../cashier/pendingOperations";

/** An in-memory Web Locks manager shared by simulated tabs (exclusive locks only). */
export class FakeLockManager implements LockManagerLike {
  readonly requested: string[] = [];
  private readonly held = new Set<string>();
  private readonly waiting = new Map<string, Array<() => void>>();

  async request<T>(
    name: string,
    options: { mode?: "exclusive"; ifAvailable?: boolean },
    callback: (lock: unknown) => Promise<T> | T,
  ): Promise<T> {
    this.requested.push(name);
    if (options.ifAvailable && this.held.has(name)) return callback(null);
    while (this.held.has(name)) {
      await new Promise<void>((resolve) => {
        const queue = this.waiting.get(name) ?? [];
        queue.push(resolve);
        this.waiting.set(name, queue);
      });
    }
    this.held.add(name);
    try {
      return await callback({ name });
    } finally {
      this.held.delete(name);
      this.waiting.get(name)?.shift()?.();
    }
  }

  isHeld(name: string): boolean {
    return this.held.has(name);
  }

  async query(): Promise<{ held: Array<{ name: string }> }> {
    return { held: [...this.held].map((name) => ({ name })) };
  }
}
