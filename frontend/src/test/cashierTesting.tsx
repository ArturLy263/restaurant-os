import { render, screen } from "@testing-library/react";

import { App } from "../App";
import type { CurrentUser } from "../api/types";
import { writeToken } from "../auth/tokenStorage";
import type { CashierSectionId } from "../cashier/sections";
import { fakeToken, jsonResponse, mockApi } from "./fixtures";

type Route = Response | ((init: RequestInit | undefined) => Response | Promise<Response>);

/**
 * Signs `user` in on the cashier workspace with `section` open, the API
 * mocked by `routes` ("METHOD /path" → responses in order, the last one
 * repeating). Returns the recorded calls.
 */
export function openCashierSection(user: CurrentUser, section: CashierSectionId, routes: Record<string, Route[]>) {
  window.sessionStorage.setItem("restaurantos.mode", "cashier");
  window.sessionStorage.setItem("restaurantos.cashier.section", section);
  writeToken(fakeToken({ sub: user.id, exp: Math.floor(Date.now() / 1000) + 3600 }));
  const api = mockApi({
    "GET /api/v1/me": [jsonResponse(user, 200, { Date: new Date().toUTCString() })],
    ...routes,
  });
  render(<App />);
  return api;
}

/** The JSON body and Idempotency-Key of each call to `key`. */
export function sent(calls: Array<{ key: string; init: RequestInit | undefined }>, key: string) {
  return calls
    .filter((call) => call.key === key)
    .map((call) => ({
      body: call.init?.body === undefined ? undefined : JSON.parse(String(call.init.body)),
      key: (call.init?.headers as Record<string, string> | undefined)?.["Idempotency-Key"],
    }));
}

/** A response that arrives only when `release()` is called. */
export function deferred(response: Response) {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  return {
    handler: async () => {
      await gate;
      return response.clone();
    },
    release: () => release(),
  };
}

export async function findSection(title: string) {
  return screen.findByRole("heading", { level: 2, name: title });
}
