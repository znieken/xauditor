import type { Role } from "@/lib/types";

const TIER_RANK: Record<Role, number> = {
  admin: 3,
  auditor: 2,
  viewer: 1,
};

/**
 * `tierSatisfies(userRole, requiredRole)` answers: does the user's role
 * satisfy a tab/control's `requiredRole` field?
 *
 * Tiering rule: admin satisfies any tier; auditor satisfies auditor +
 * viewer; viewer satisfies viewer only. The role names are the same set
 * the backend's `Role` enum carries.
 */
export function tierSatisfies(userRole: Role, requiredRole: Role): boolean {
  return TIER_RANK[userRole] >= TIER_RANK[requiredRole];
}

export function isWriter(role: Role): boolean {
  return role === "admin" || role === "auditor";
}

export function isAdmin(role: Role): boolean {
  return role === "admin";
}
