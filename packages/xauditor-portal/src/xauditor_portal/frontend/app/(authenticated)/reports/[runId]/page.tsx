"use client";

import { useRouter } from "next/navigation";
import * as React from "react";

export default function LegacyRunDetailRedirect({
  params,
}: {
  params: { runId: string };
}) {
  const router = useRouter();
  React.useEffect(() => {
    router.replace(`/reports/runs/${params.runId}`);
  }, [params.runId, router]);
  return (
    <p className="text-sm text-zinc-500 dark:text-zinc-400">
      Redirecting to new run URL…
    </p>
  );
}
