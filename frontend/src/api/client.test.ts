import { describe, expect, it, vi } from "vitest";

import { errorResponse, jsonResponse, mockApi } from "../test/fixtures";
import { ApiError, NETWORK_ERROR, apiRequest, isApiError } from "./client";

describe("apiRequest", () => {
  it("returns the parsed body and sends the bearer token", async () => {
    const { calls } = mockApi({ "GET /api/v1/me": [jsonResponse({ id: "u1" })] });

    await expect(apiRequest("/api/v1/me", { token: "abc" })).resolves.toEqual({ id: "u1" });

    const headers = calls[0].init?.headers as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer abc");
    expect(headers["Content-Type"]).toBeUndefined();
  });

  it("sends a JSON body", async () => {
    const { calls } = mockApi({
      "POST /api/v1/auth/login": [jsonResponse({ access_token: "t", token_type: "bearer" })],
    });

    await apiRequest("/api/v1/auth/login", { method: "POST", body: { email: "a@b.c", password: "x" } });

    expect(calls[0].init?.body).toBe(JSON.stringify({ email: "a@b.c", password: "x" }));
    expect((calls[0].init?.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
  });

  it.each([
    [401, "NOT_AUTHENTICATED", "Could not validate credentials", "isUnauthorized"],
    [403, "STATION_ACCESS_DENIED", "Station not accessible", "isForbidden"],
    [409, "INVALID_PREPARATION_TRANSITION", "Invalid preparation transition: READY -> READY", "isConflict"],
  ] as const)("normalizes a %i error envelope", async (status, code, message, flag) => {
    mockApi({ "GET /x": [errorResponse(status, code, message)] });

    const error = await apiRequest("/x").catch((caught: unknown) => caught);

    expect(isApiError(error)).toBe(true);
    const apiError = error as ApiError;
    expect(apiError.status).toBe(status);
    expect(apiError.code).toBe(code);
    expect(apiError.message).toBe(message);
    expect(apiError.requestId).toBe("req-123");
    expect(apiError[flag]).toBe(true);
  });

  it("keeps the field errors of a validation error as detail", async () => {
    const detail = [{ loc: ["body", "email"], msg: "value is not a valid email address" }];
    mockApi({
      "POST /x": [
        jsonResponse(
          { detail, error: { code: "VALIDATION_ERROR", message: "Request validation failed", request_id: "r" } },
          422,
        ),
      ],
    });

    const error = (await apiRequest("/x", { method: "POST", body: {} }).catch((e: unknown) => e)) as ApiError;

    expect(error.code).toBe("VALIDATION_ERROR");
    expect(error.detail).toEqual(detail);
  });

  it("falls back to a readable message for a response without an envelope", async () => {
    mockApi({
      "GET /x": [new Response("<html>Bad Gateway</html>", { status: 502, headers: { "X-Request-ID": "gw" } })],
    });

    const error = (await apiRequest("/x").catch((e: unknown) => e)) as ApiError;

    expect(error.status).toBe(502);
    expect(error.code).toBe("HTTP_502");
    expect(error.message).toBe("The RestaurantOS server is not reachable.");
    expect(error.requestId).toBe("gw");
  });

  it("reports a network failure as NETWORK_ERROR", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    const error = (await apiRequest("/x").catch((e: unknown) => e)) as ApiError;

    expect(error.status).toBe(0);
    expect(error.code).toBe(NETWORK_ERROR);
  });

  it("rethrows an abort unchanged", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new DOMException("aborted", "AbortError")));

    await expect(apiRequest("/x")).rejects.toMatchObject({ name: "AbortError" });
  });
});
