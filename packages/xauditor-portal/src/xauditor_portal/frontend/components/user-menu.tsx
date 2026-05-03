"use client";

import { useQueryClient } from "@tanstack/react-query";
import { LogOut, UserRound } from "lucide-react";
import { useRouter } from "next/navigation";
import * as React from "react";
import { api } from "@/lib/api";
import type { Role } from "@/lib/types";
import { Button } from "@/components/ui/button";

export function UserMenu({
  username,
  role,
}: {
  username: string;
  role?: Role;
}) {
  const router = useRouter();
  const qc = useQueryClient();
  const [busy, setBusy] = React.useState(false);

  async function handleLogout() {
    setBusy(true);
    try {
      await api.logout();
    } catch {
      // ignore — even if the call fails we're clearing state client-side.
    }
    qc.clear();
    router.replace("/login");
  }

  return (
    <div className="flex items-center gap-2">
      <div className="flex items-center gap-2 rounded-md border border-zinc-200 bg-white px-2.5 py-1 text-xs dark:border-zinc-800 dark:bg-zinc-900">
        <UserRound className="h-3.5 w-3.5 text-zinc-500" aria-hidden />
        <span className="font-medium">{username}</span>
        {role ? (
          <span
            className="rounded-sm border border-zinc-200 bg-zinc-50 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-zinc-600 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-400"
            title={`Role: ${role}`}
          >
            {role}
          </span>
        ) : null}
      </div>
      <Button
        variant="ghost"
        size="icon"
        onClick={handleLogout}
        disabled={busy}
        aria-label="Sign out"
        title="Sign out"
      >
        <LogOut className="h-4 w-4" aria-hidden />
      </Button>
    </div>
  );
}
