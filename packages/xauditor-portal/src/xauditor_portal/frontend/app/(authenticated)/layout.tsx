"use client";

import { useRouter } from "next/navigation";
import * as React from "react";
import { ApiError } from "@/lib/api";
import { useMe } from "@/lib/api";
import { PasswordChangeModal } from "@/components/password-change-modal";
import { Sidebar } from "@/components/sidebar";
import { ThemeToggle } from "@/components/theme-toggle";
import { UserMenu } from "@/components/user-menu";

export default function AuthenticatedLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const router = useRouter();
  const { data: me, isLoading, error } = useMe();

  React.useEffect(() => {
    if (error instanceof ApiError && error.status === 401) {
      const next = encodeURIComponent(
        typeof window !== "undefined"
          ? window.location.pathname + window.location.search
          : "/reports",
      );
      router.replace(`/login?next=${next}`);
    }
  }, [error, router]);

  if (isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center text-xs text-zinc-500 dark:text-zinc-400">
        Loading…
      </div>
    );
  }
  if (!me) {
    // The effect above is handling redirection; render nothing meanwhile.
    return null;
  }

  const forced = me.must_change_password;

  // First-login gate. While `must_change_password` is true, render the
  // password-change modal as the only UI — do NOT mount the sidebar, the
  // header, or {children}. Mounting children would let data-fetching hooks
  // (e.g. useProjects) fire, hit `forbid_must_change_password`-gated
  // endpoints, and surface 403-derived error toasts behind the modal.
  // Once the modal succeeds it invalidates the `me` query, this layout
  // re-renders with `forced === false`, and the children mount cleanly.
  if (forced) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-zinc-50 text-zinc-900 dark:bg-zinc-950 dark:text-zinc-100">
        <PasswordChangeModal />
      </div>
    );
  }

  return (
    <div className="flex min-h-screen bg-zinc-50 text-zinc-900 dark:bg-zinc-950 dark:text-zinc-100">
      <div className="flex flex-1">
        <Sidebar />
        <div className="flex flex-1 flex-col">
          <header className="flex h-14 items-center justify-between border-b border-zinc-200 bg-white/70 px-6 backdrop-blur dark:border-zinc-800 dark:bg-zinc-950/60">
            <div className="text-sm text-zinc-500 dark:text-zinc-400">
              Signed in as{" "}
              <span className="font-medium text-zinc-900 dark:text-zinc-100">
                {me.username}
              </span>
            </div>
            <div className="flex items-center gap-2">
              <ThemeToggle />
              <UserMenu username={me.username} role={me.role} />
            </div>
          </header>
          <main className="flex-1 overflow-auto px-6 py-6">
            <div className="mx-auto max-w-7xl">{children}</div>
          </main>
        </div>
      </div>
    </div>
  );
}
