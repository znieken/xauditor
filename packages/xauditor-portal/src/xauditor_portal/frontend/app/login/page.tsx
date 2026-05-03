"use client";

import { useRouter, useSearchParams } from "next/navigation";
import * as React from "react";
import { Suspense } from "react";
import { ApiError, api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";

function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const redirectTo = searchParams.get("next") || "/reports";
  const [username, setUsername] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [submitting, setSubmitting] = React.useState(false);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await api.login(username, password);
      router.replace(redirectTo);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Unexpected login failure.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="relative flex min-h-screen items-center justify-center px-6">
      <div
        className="pointer-events-none absolute inset-0 -z-10 bg-[radial-gradient(60%_50%_at_50%_0%,rgba(99,102,241,0.18),transparent),radial-gradient(40%_40%_at_80%_60%,rgba(16,185,129,0.14),transparent)] dark:bg-[radial-gradient(60%_50%_at_50%_0%,rgba(99,102,241,0.15),transparent),radial-gradient(40%_40%_at_80%_60%,rgba(16,185,129,0.10),transparent)]"
        aria-hidden
      />
      <Card className="w-full max-w-sm shadow-lg">
        <CardHeader>
          <div className="mb-2 inline-flex h-8 w-8 items-center justify-center rounded-lg bg-zinc-900 text-xs font-bold text-zinc-50 dark:bg-zinc-50 dark:text-zinc-900">
            xA
          </div>
          <CardTitle>xauditor Portal</CardTitle>
          <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-400">
            Sign in with your auditor credentials to continue.
          </p>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-3">
            <div className="space-y-1">
              <Label htmlFor="username">Username</Label>
              <Input
                id="username"
                name="username"
                autoComplete="username"
                required
                autoFocus
                value={username}
                onChange={(event) => setUsername(event.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="password">Password</Label>
              <Input
                id="password"
                name="password"
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
            </div>
            {error ? (
              <p
                role="alert"
                className="rounded-md bg-rose-50 px-3 py-2 text-xs text-rose-700 dark:bg-rose-950 dark:text-rose-200"
              >
                {error}
              </p>
            ) : null}
            <Button type="submit" className="w-full" disabled={submitting}>
              {submitting ? "Signing in…" : "Sign in"}
            </Button>
          </form>
          <p className="mt-5 text-xs text-zinc-500 dark:text-zinc-400">
            Default credentials on first boot:{" "}
            <span className="font-mono">admin / admin</span>. You will be
            asked to rotate the password immediately after sign-in.
          </p>
        </CardContent>
      </Card>
    </main>
  );
}

export default function LoginPage() {
  return (
    <Suspense
      fallback={
        <main className="flex min-h-screen items-center justify-center text-xs text-zinc-500 dark:text-zinc-400">
          Loading…
        </main>
      }
    >
      <LoginForm />
    </Suspense>
  );
}
