import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { App } from "./App";
import { writeToken } from "./auth/tokenStorage";
import {
  BAR,
  KITCHEN_USER,
  STATION,
  errorResponse,
  fakeToken,
  jsonResponse,
  mockApi,
  queueItem,
} from "./test/fixtures";

const TOKEN = fakeToken({ sub: KITCHEN_USER.id, exp: Math.floor(Date.now() / 1000) + 3600 });
const QUEUE = `GET /api/v1/stations/${STATION.id}/preparations`;

async function signIn() {
  render(<App />);
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: "kim@example.com" } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "secret" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
}

describe("App", () => {
  it("signs in, loads /me and stations, and shows the only station's queue", async () => {
    const { calls } = mockApi({
      "POST /api/v1/auth/login": [jsonResponse({ access_token: TOKEN, token_type: "bearer" })],
      "GET /api/v1/me": [jsonResponse(KITCHEN_USER)],
      "GET /api/v1/stations": [jsonResponse([STATION])],
      [QUEUE]: [jsonResponse([queueItem({ product_name: "Burger", table_number: 4 })])],
    });

    await signIn();

    expect(await screen.findByText("Burger")).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Table 4" })).toBeTruthy();
    expect(screen.getByText("Kim Cook")).toBeTruthy();
    expect(calls.map((call) => call.key)).toEqual([
      "POST /api/v1/auth/login",
      "GET /api/v1/me",
      "GET /api/v1/stations",
      QUEUE,
    ]);
    expect(window.sessionStorage.getItem("restaurantos.accessToken")).toBe(TOKEN);
  });

  it("shows the login error for invalid credentials", async () => {
    mockApi({
      "POST /api/v1/auth/login": [errorResponse(401, "INVALID_CREDENTIALS", "Invalid email or password")],
    });

    await signIn();

    expect((await screen.findByRole("alert")).textContent).toBe("Invalid email or password.");
    expect(window.sessionStorage.getItem("restaurantos.accessToken")).toBeNull();
  });

  it("lets the user choose among several stations", async () => {
    writeToken(TOKEN);
    mockApi({
      "GET /api/v1/me": [jsonResponse({ ...KITCHEN_USER, stations: null })],
      "GET /api/v1/stations": [jsonResponse([BAR, { ...STATION, is_active: false }])],
      [QUEUE]: [jsonResponse([])],
    });

    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /Kitchen.*Inactive/ }));

    expect(await screen.findByText("All caught up")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Change station" })).toBeTruthy();
  });

  it("starts an item and refetches the queue", async () => {
    writeToken(TOKEN);
    const item = queueItem({ product_name: "Pasta" });
    const { calls } = mockApi({
      "GET /api/v1/me": [jsonResponse(KITCHEN_USER)],
      "GET /api/v1/stations": [jsonResponse([STATION])],
      [QUEUE]: [jsonResponse([item]), jsonResponse([{ ...item, status: "IN_PROGRESS", started_at: "2026-10-22T18:05:00" }])],
      [`POST /api/v1/preparations/${item.id}/start`]: [jsonResponse({ ...item, status: "IN_PROGRESS" })],
    });

    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Start: 1× Pasta" }));

    const preparing = screen.getByRole("region", { name: "Preparing" });
    expect(await within(preparing).findByRole("button", { name: "Ready: 1× Pasta" })).toBeTruthy();
    expect(calls.map((call) => call.key).slice(-2)).toEqual([`POST /api/v1/preparations/${item.id}/start`, QUEUE]);
    expect((calls.at(-2)!.init?.headers as Record<string, string>).Authorization).toBe(`Bearer ${TOKEN}`);
  });

  it("explains a 409 conflict and refetches the queue", async () => {
    writeToken(TOKEN);
    const item = queueItem({ product_name: "Pasta" });
    const { calls } = mockApi({
      "GET /api/v1/me": [jsonResponse(KITCHEN_USER)],
      "GET /api/v1/stations": [jsonResponse([STATION])],
      [QUEUE]: [jsonResponse([item]), jsonResponse([])],
      [`POST /api/v1/preparations/${item.id}/start`]: [
        errorResponse(409, "INVALID_PREPARATION_TRANSITION", "Invalid preparation transition: CANCELLED -> IN_PROGRESS"),
      ],
    });

    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Start: 1× Pasta" }));

    expect((await screen.findByRole("alert")).textContent).toContain("already updated or cancelled elsewhere");
    expect(await screen.findByText("All caught up")).toBeTruthy();
    expect(calls.at(-1)!.key).toBe(QUEUE);
  });

  it("shows a 403 on an action without leaving the queue", async () => {
    writeToken(TOKEN);
    const item = queueItem({ product_name: "Pasta" });
    mockApi({
      "GET /api/v1/me": [jsonResponse(KITCHEN_USER)],
      "GET /api/v1/stations": [jsonResponse([STATION])],
      [QUEUE]: [jsonResponse([item])],
      [`POST /api/v1/preparations/${item.id}/start`]: [
        errorResponse(403, "STATION_ACCESS_DENIED", "Station not accessible"),
      ],
    });

    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Start: 1× Pasta" }));

    expect((await screen.findByRole("alert")).textContent).toContain("not allowed to start 1× Pasta: Station not accessible");
    expect(screen.getByText("Pasta")).toBeTruthy();
  });

  it("shows a forbidden queue without a retry button", async () => {
    writeToken(TOKEN);
    mockApi({
      "GET /api/v1/me": [jsonResponse(KITCHEN_USER)],
      "GET /api/v1/stations": [jsonResponse([STATION])],
      [QUEUE]: [errorResponse(403, "STATION_ACCESS_DENIED", "Station not accessible")],
    });

    render(<App />);

    expect(await screen.findByRole("heading", { name: "No access to Kitchen" })).toBeTruthy();
    expect(screen.getByText("Request ID: req-123")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });

  it("shows a station list error with retry", async () => {
    writeToken(TOKEN);
    mockApi({
      "GET /api/v1/me": [jsonResponse(KITCHEN_USER)],
      "GET /api/v1/stations": [errorResponse(500, "INTERNAL_ERROR", "Internal server error"), jsonResponse([])],
    });

    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Try again" }));

    expect(await screen.findByText("No stations available")).toBeTruthy();
  });

  it("signs out on a 401 and asks to sign in again", async () => {
    writeToken(TOKEN);
    mockApi({
      "GET /api/v1/me": [jsonResponse(KITCHEN_USER)],
      "GET /api/v1/stations": [jsonResponse([STATION])],
      [QUEUE]: [errorResponse(401, "NOT_AUTHENTICATED", "Could not validate credentials")],
    });

    render(<App />);

    expect((await screen.findByRole("status")).textContent).toContain("session has expired");
    expect(screen.getByRole("button", { name: "Sign in" })).toBeTruthy();
    expect(window.sessionStorage.getItem("restaurantos.accessToken")).toBeNull();
  });

  it("drops a stored token that /me rejects", async () => {
    writeToken("stale");
    mockApi({ "GET /api/v1/me": [errorResponse(401, "NOT_AUTHENTICATED", "Could not validate credentials")] });

    render(<App />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Sign in" })).toBeTruthy());
    expect(window.sessionStorage.getItem("restaurantos.accessToken")).toBeNull();
  });
});
