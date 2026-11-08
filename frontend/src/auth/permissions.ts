import type { CurrentUser, PermissionName } from "../api/types";

// Permission checks on the permissions GET /me returned. They only decide
// what the interface offers: the backend checks every request itself, so a
// hidden button is a convenience, never the protection.

type PermissionHolder = Pick<CurrentUser, "permissions">;

export function hasPermission(user: PermissionHolder, permission: PermissionName): boolean {
  return user.permissions.includes(permission);
}

export function hasAllPermissions(user: PermissionHolder, permissions: readonly PermissionName[]): boolean {
  return permissions.every((permission) => hasPermission(user, permission));
}

export function hasAnyPermission(user: PermissionHolder, permissions: readonly PermissionName[]): boolean {
  return permissions.some((permission) => hasPermission(user, permission));
}

/** A requirement: every permission of `all`, and at least one of `any` (if given). */
export interface PermissionRequirement {
  all?: readonly PermissionName[];
  any?: readonly PermissionName[];
}

export function meetsRequirement(user: PermissionHolder, requirement: PermissionRequirement): boolean {
  const { all = [], any } = requirement;
  return hasAllPermissions(user, all) && (any === undefined || any.length === 0 || hasAnyPermission(user, any));
}
