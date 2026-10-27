import { describe, expect, it } from "vitest";

import { fakeToken } from "../test/fixtures";
import { clearToken, readToken, tokenExpiresAt, writeToken } from "./tokenStorage";

describe("token storage", () => {
  it("keeps the token in sessionStorage", () => {
    writeToken("abc");
    expect(readToken()).toBe("abc");
    expect(window.localStorage.length).toBe(0);
    clearToken();
    expect(readToken()).toBeNull();
  });

  it("reads exp from the token payload", () => {
    expect(tokenExpiresAt(fakeToken({ sub: "u", exp: 1_800_000_000 }))).toBe(1_800_000_000_000);
  });

  it("returns null for tokens without a readable exp", () => {
    expect(tokenExpiresAt(fakeToken({ sub: "u" }))).toBeNull();
    expect(tokenExpiresAt("not-a-jwt")).toBeNull();
    expect(tokenExpiresAt("a.%%%.c")).toBeNull();
  });
});
