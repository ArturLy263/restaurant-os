import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

import { FakeWebSocket } from "./fakeSocket";

// No test opens a real connection; vitest unstubs globals after each test.
beforeEach(() => {
  FakeWebSocket.reset();
  vi.stubGlobal("WebSocket", FakeWebSocket);
});

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
});
