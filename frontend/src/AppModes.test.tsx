import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { App, availableModes } from "./App";
import { writeToken } from "./auth/tokenStorage";
import { KITCHEN_USER, STATION, fakeToken, jsonResponse, mockApi } from "./test/fixtures";
import { MENU, TABLES, WAITER } from "./test/posFixtures";

const TOKEN = fakeToken({ sub: WAITER.id, exp: Math.floor(Date.now() / 1000) + 3600 });
const MANAGER = {
  ...WAITER,
  role: { id: "r", name: "MANAGER" },
  permissions: [...WAITER.permissions, "orders:manage_all", "preparations:update", "preparations:view"],
};

describe("Screens by role", () => {
  it("picks screens from permissions", () => {
    expect(availableModes(WAITER)).toEqual(["pos"]);
    expect(availableModes(KITCHEN_USER)).toEqual(["kds"]);
    expect(availableModes(MANAGER)).toEqual(["pos", "kds"]);
    expect(availableModes({ ...WAITER, permissions: ["orders:view", "bills:view"] })).toEqual([]);
  });

  it("takes a waiter straight to the tables, without a screen switch", async () => {
    writeToken(TOKEN);
    mockApi({
      "GET /api/v1/me": [jsonResponse(WAITER)],
      "GET /api/v1/tables": [jsonResponse(TABLES)],
    });

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Tables" })).toBeTruthy();
    expect(screen.queryByRole("navigation", { name: "Screens" })).toBeNull();
  });

  it("lets a manager switch between the POS and the kitchen display", async () => {
    writeToken(TOKEN);
    mockApi({
      "GET /api/v1/me": [jsonResponse(MANAGER)],
      "GET /api/v1/tables": [jsonResponse(TABLES)],
      "GET /api/v1/menu": [jsonResponse(MENU)],
      "GET /api/v1/stations": [jsonResponse([STATION])],
      [`GET /api/v1/stations/${STATION.id}/preparations`]: [jsonResponse([])],
    });

    render(<App />);
    expect(await screen.findByRole("heading", { name: "Tables" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Kitchen display" }));

    expect(await screen.findByText("All caught up")).toBeTruthy();
    expect(window.sessionStorage.getItem("restaurantos.mode")).toBe("kds");
    fireEvent.click(screen.getByRole("button", { name: "Waiter POS" }));
    expect(await screen.findByRole("heading", { name: "Tables" })).toBeTruthy();
  });

  it("explains when a role has no screen", async () => {
    writeToken(TOKEN);
    mockApi({
      "GET /api/v1/me": [jsonResponse({ ...WAITER, role: { id: "c", name: "CASHIER" }, permissions: ["orders:view"] })],
    });

    render(<App />);

    expect(await screen.findByText("No screen for your role yet")).toBeTruthy();
  });
});
