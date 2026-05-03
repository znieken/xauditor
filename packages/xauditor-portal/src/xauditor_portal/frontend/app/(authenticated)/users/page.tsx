"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Trash2, KeyRound, Ban, CheckCircle2 } from "lucide-react";
import * as React from "react";
import { ApiError, api, useMe, useUsers } from "@/lib/api";
import type { Role, UserSummary } from "@/lib/types";
import { ConfirmActionDialog } from "@/components/confirm-action-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input, Label, Select } from "@/components/ui/input";

const ROLES: readonly Role[] = ["admin", "auditor", "viewer"] as const;

export default function UsersPage() {
  const { data: me, isLoading: meLoading } = useMe();
  const isAdmin = me?.role === "admin";
  const { data, refetch, isLoading } = useUsers(isAdmin);

  if (meLoading) {
    return (
      <div className="text-sm text-zinc-500 dark:text-zinc-400">Loading…</div>
    );
  }

  if (!isAdmin) {
    return (
      <Card className="p-6">
        <h1 className="text-lg font-semibold text-zinc-900 dark:text-zinc-100">
          Forbidden
        </h1>
        <p className="mt-2 text-sm text-zinc-600 dark:text-zinc-400">
          The Users tab is available to administrators only. Ask your portal
          admin to upgrade your role if you need access.
        </p>
      </Card>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
          Users
        </h1>
        <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
          Manage portal accounts. Admins can full-manage; auditors can label
          findings and write notes; viewers have read-only access.
        </p>
      </div>

      <CreateUserCard onCreated={() => refetch()} />

      <Card className="overflow-hidden">
        <div className="border-b border-zinc-200 bg-zinc-50/50 px-4 py-2 text-xs font-medium uppercase tracking-wide text-zinc-500 dark:border-zinc-800 dark:bg-zinc-900/40 dark:text-zinc-400">
          Existing users
        </div>
        {isLoading ? (
          <div className="px-4 py-6 text-sm text-zinc-500 dark:text-zinc-400">
            Loading users…
          </div>
        ) : (
          <UsersTable
            users={data?.items ?? []}
            currentUserId={me?.id}
            onChanged={() => refetch()}
          />
        )}
      </Card>
    </div>
  );
}

function CreateUserCard({ onCreated }: { onCreated: () => void }) {
  const [username, setUsername] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [role, setRole] = React.useState<Role>("auditor");
  const [error, setError] = React.useState<string | null>(null);

  const create = useMutation({
    mutationFn: () => api.createUser(username.trim(), password, role),
    onSuccess: () => {
      setUsername("");
      setPassword("");
      setRole("auditor");
      setError(null);
      onCreated();
    },
    onError: (err) => {
      setError(err instanceof ApiError ? err.message : "Failed to create user");
    },
  });

  return (
    <Card className="p-4">
      <h2 className="mb-3 text-sm font-semibold text-zinc-900 dark:text-zinc-100">
        Create user
      </h2>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (!username.trim() || !password) return;
          create.mutate();
        }}
        className="grid grid-cols-1 gap-3 sm:grid-cols-[1fr_1fr_auto_auto]"
      >
        <div>
          <Label htmlFor="new-username">Username</Label>
          <Input
            id="new-username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="off"
            required
          />
        </div>
        <div>
          <Label htmlFor="new-password">Initial password</Label>
          <Input
            id="new-password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="new-password"
            required
          />
        </div>
        <div>
          <Label htmlFor="new-role">Role</Label>
          <Select
            id="new-role"
            value={role}
            onChange={(e) => setRole(e.target.value as Role)}
          >
            {ROLES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </Select>
        </div>
        <div className="flex items-end">
          <Button
            type="submit"
            disabled={create.isPending || !username.trim() || !password}
          >
            {create.isPending ? "Creating…" : "Create"}
          </Button>
        </div>
      </form>
      {error ? (
        <p className="mt-2 text-xs text-rose-600 dark:text-rose-400">{error}</p>
      ) : null}
      <p className="mt-2 text-xs text-zinc-500 dark:text-zinc-400">
        Password must be at least 12 characters and contain a letter and a
        digit. The new user will be forced to change it on first login.
      </p>
    </Card>
  );
}

function UsersTable({
  users,
  currentUserId,
  onChanged,
}: {
  users: UserSummary[];
  currentUserId: string | undefined;
  onChanged: () => void;
}) {
  // Match the backend invariant: enabled admins (role === 'admin' AND
  // disabled_at == null) are the ones that satisfy the at-least-one-admin
  // guard. A disabled admin does not count.
  const enabledAdminCount = users.filter(
    (u) => u.role === "admin" && !u.disabled_at,
  ).length;
  return (
    <table className="w-full text-sm">
      <thead className="text-left text-xs uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
        <tr>
          <th className="px-4 py-2 font-medium">Username</th>
          <th className="px-4 py-2 font-medium">Role</th>
          <th className="px-4 py-2 font-medium">Status</th>
          <th className="px-4 py-2 font-medium">Created</th>
          <th className="px-4 py-2 text-right font-medium">Actions</th>
        </tr>
      </thead>
      <tbody>
        {users.map((u) => (
          <UserRow
            key={u.id}
            user={u}
            isSelf={u.id === currentUserId}
            isLastAdmin={
              u.role === "admin" && !u.disabled_at && enabledAdminCount <= 1
            }
            onChanged={onChanged}
          />
        ))}
      </tbody>
    </table>
  );
}

function UserRow({
  user,
  isSelf,
  isLastAdmin,
  onChanged,
}: {
  user: UserSummary;
  isSelf: boolean;
  isLastAdmin: boolean;
  onChanged: () => void;
}) {
  const qc = useQueryClient();
  const [resetting, setResetting] = React.useState(false);
  const [tempPassword, setTempPassword] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [pendingAction, setPendingAction] = React.useState<
    "disable" | "delete" | null
  >(null);

  const updateRole = useMutation({
    mutationFn: (role: Role) => api.updateUser(user.id, { role }),
    onSuccess: () => {
      setError(null);
      qc.invalidateQueries({ queryKey: ["users"] });
      onChanged();
    },
    onError: (err) => {
      setError(err instanceof ApiError ? err.message : "Failed to update role");
    },
  });

  const reset = useMutation({
    mutationFn: () => api.resetUserPassword(user.id, tempPassword),
    onSuccess: () => {
      setResetting(false);
      setTempPassword("");
      setError(null);
      onChanged();
    },
    onError: (err) => {
      setError(err instanceof ApiError ? err.message : "Failed to reset password");
    },
  });

  const del = useMutation({
    mutationFn: () => api.deleteUser(user.id),
    onSuccess: () => {
      setError(null);
      onChanged();
    },
    onError: (err) => {
      setError(err instanceof ApiError ? err.message : "Failed to delete user");
    },
  });

  const disable = useMutation({
    mutationFn: () => api.disableUser(user.id),
    onSuccess: () => {
      setError(null);
      qc.invalidateQueries({ queryKey: ["users"] });
      onChanged();
    },
    onError: (err) => {
      setError(
        err instanceof ApiError ? err.message : "Failed to disable user",
      );
    },
  });

  const enable = useMutation({
    mutationFn: () => api.enableUser(user.id),
    onSuccess: () => {
      setError(null);
      qc.invalidateQueries({ queryKey: ["users"] });
      onChanged();
    },
    onError: (err) => {
      setError(
        err instanceof ApiError ? err.message : "Failed to enable user",
      );
    },
  });

  const isDisabled = user.disabled_at !== null;
  const lastAdminLockedRoleSelect = isLastAdmin && user.role === "admin";
  // Admins cannot delete their own account — even when other admins exist —
  // because the session-revoke + cascade would log them out mid-action and
  // strand the UI in a broken state. The API enforces the same rule.
  const deleteDisabled = lastAdminLockedRoleSelect || isSelf;
  const deleteDisabledReason = isSelf
    ? "You cannot delete your own account"
    : lastAdminLockedRoleSelect
    ? "Cannot delete the last admin"
    : "Delete user";

  return (
    <>
      <tr className="border-t border-zinc-100 dark:border-zinc-800">
        <td className="px-4 py-2">
          <div className="flex items-center gap-2">
            <span className="font-medium text-zinc-900 dark:text-zinc-100">
              {user.username}
            </span>
            {isSelf ? (
              <Badge tone="neutral" className="text-[10px]">
                you
              </Badge>
            ) : null}
          </div>
        </td>
        <td className="px-4 py-2">
          {isSelf ? (
            <span
              className="inline-flex items-center gap-2 text-xs text-zinc-500 dark:text-zinc-400"
              title="Ask another admin to change your role"
            >
              <span className="font-medium text-zinc-700 dark:text-zinc-300">
                {user.role}
              </span>
              <Badge tone="neutral" className="text-[10px]">
                self · ask peer admin
              </Badge>
            </span>
          ) : (
            <Select
              value={user.role}
              onChange={(e) => updateRole.mutate(e.target.value as Role)}
              disabled={updateRole.isPending || isDisabled}
              title={
                lastAdminLockedRoleSelect
                  ? "Cannot demote the last admin"
                  : isDisabled
                  ? "Re-enable the user before changing their role"
                  : undefined
              }
            >
              {ROLES.map((r) => (
                <option
                  key={r}
                  value={r}
                  disabled={lastAdminLockedRoleSelect && r !== "admin"}
                >
                  {r}
                </option>
              ))}
            </Select>
          )}
        </td>
        <td className="px-4 py-2">
          {isDisabled ? (
            <span className="inline-flex items-center gap-2">
              <Badge tone="danger">disabled</Badge>
              <span
                className="text-[11px] text-zinc-500 dark:text-zinc-400"
                title={user.disabled_at ?? undefined}
              >
                {user.disabled_at
                  ? new Date(user.disabled_at).toLocaleString()
                  : ""}
              </span>
            </span>
          ) : user.must_change_password ? (
            <Badge tone="warning">must rotate</Badge>
          ) : (
            <Badge tone="success">active</Badge>
          )}
        </td>
        <td className="px-4 py-2 text-xs text-zinc-500 dark:text-zinc-400">
          {new Date(user.created_at).toLocaleString()}
        </td>
        <td className="px-4 py-2 text-right">
          <div className="inline-flex items-center gap-1">
            <Button
              variant="ghost"
              size="icon"
              aria-label="Reset password"
              title="Reset password"
              onClick={() => setResetting((r) => !r)}
            >
              <KeyRound className="h-4 w-4" aria-hidden />
            </Button>
            {isDisabled ? (
              <Button
                variant="ghost"
                size="icon"
                aria-label="Enable user"
                title="Enable user"
                onClick={() => enable.mutate()}
                disabled={enable.isPending}
              >
                <CheckCircle2 className="h-4 w-4" aria-hidden />
              </Button>
            ) : (
              <Button
                variant="ghost"
                size="icon"
                aria-label="Disable user"
                title={
                  isSelf
                    ? "Disable your own account (your active session will end)"
                    : "Disable user (their active session will end)"
                }
                onClick={() => setPendingAction("disable")}
                disabled={disable.isPending}
              >
                <Ban className="h-4 w-4" aria-hidden />
              </Button>
            )}
            <Button
              variant="ghost"
              size="icon"
              aria-label="Delete user"
              title={deleteDisabledReason}
              onClick={() => {
                if (deleteDisabled) return;
                setPendingAction("delete");
              }}
              disabled={deleteDisabled || del.isPending}
            >
              <Trash2 className="h-4 w-4" aria-hidden />
            </Button>
            {pendingAction === "disable" ? (
              <ConfirmActionDialog
                title="Disable user"
                subtitle={
                  <>
                    <span className="font-medium text-zinc-700 dark:text-zinc-300">
                      {user.username}
                    </span>
                    <span className="ml-1">· role: {user.role}</span>
                    {isSelf ? (
                      <span className="ml-1">· this is your account</span>
                    ) : null}
                  </>
                }
                description={
                  isSelf ? (
                    <>
                      Disabling your own account will revoke your active
                      session immediately and you will not be able to log
                      back in until another admin re-enables your account.
                      Make sure you have coordinated with a peer admin
                      before continuing.
                    </>
                  ) : (
                    <>
                      The user will be unable to log in until an admin
                      re-enables the account. Their currently active
                      session (if any) will be rejected on its next
                      request, forcing them back to the login page. The
                      account row, including username and audit history,
                      is preserved.
                    </>
                  )
                }
                confirmLabel="Disable user"
                destructive
                fallbackErrorMessage="Failed to disable user."
                onConfirm={async () => {
                  await disable.mutateAsync();
                }}
                onClose={() => setPendingAction(null)}
              />
            ) : null}
            {pendingAction === "delete" ? (
              <ConfirmActionDialog
                title="Delete user"
                subtitle={
                  <>
                    <span className="font-medium text-zinc-700 dark:text-zinc-300">
                      {user.username}
                    </span>
                    <span className="ml-1">· role: {user.role}</span>
                  </>
                }
                description={
                  <>
                    Permanently removes the user row, every
                    password-history entry, and every revoked-session
                    record for this user. Findings the user labeled
                    retain their feedback (the FK is{" "}
                    <code className="font-mono text-[11px]">SET NULL</code>),
                    but the reviewer attribution is lost. This cannot be
                    undone. Prefer <strong>Disable</strong> if you may
                    want to restore access later.
                  </>
                }
                confirmLabel="Delete user"
                destructive
                fallbackErrorMessage="Failed to delete user."
                onConfirm={async () => {
                  await del.mutateAsync();
                }}
                onClose={() => setPendingAction(null)}
              />
            ) : null}
          </div>
        </td>
      </tr>
      {resetting ? (
        <tr className="border-t border-zinc-100 dark:border-zinc-800">
          <td colSpan={5} className="px-4 py-3">
            <form
              onSubmit={(e) => {
                e.preventDefault();
                if (!tempPassword) return;
                reset.mutate();
              }}
              className="flex items-end gap-2"
            >
              <div className="flex-1 max-w-md">
                <Label htmlFor={`reset-${user.id}`}>
                  New temporary password for {user.username}
                </Label>
                <Input
                  id={`reset-${user.id}`}
                  type="password"
                  value={tempPassword}
                  onChange={(e) => setTempPassword(e.target.value)}
                  autoComplete="new-password"
                />
              </div>
              <Button
                type="submit"
                disabled={reset.isPending || !tempPassword}
              >
                {reset.isPending ? "Saving…" : "Save"}
              </Button>
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  setResetting(false);
                  setTempPassword("");
                }}
              >
                Cancel
              </Button>
            </form>
          </td>
        </tr>
      ) : null}
      {error ? (
        <tr className="border-t border-zinc-100 dark:border-zinc-800">
          <td colSpan={5} className="px-4 py-2 text-xs text-rose-600 dark:text-rose-400">
            {error}
          </td>
        </tr>
      ) : null}
    </>
  );
}
