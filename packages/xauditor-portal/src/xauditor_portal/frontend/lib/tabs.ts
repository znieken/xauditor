import type { LucideIcon } from "lucide-react";
import { FileBarChart2, UserCog } from "lucide-react";
import type { Role } from "@/lib/types";

export interface TabEntry {
  id: string;
  label: string;
  route: string;
  icon: LucideIcon;
  /**
   * Minimum role tier required to see this tab. Tabs without
   * `requiredRole` are visible to every authenticated user.
   */
  requiredRole?: Role;
}

export const TABS: readonly TabEntry[] = [
  {
    id: "report",
    label: "Reports",
    route: "/reports",
    icon: FileBarChart2,
  },
  {
    id: "users",
    label: "Users",
    route: "/users",
    icon: UserCog,
    requiredRole: "admin",
  },
];
