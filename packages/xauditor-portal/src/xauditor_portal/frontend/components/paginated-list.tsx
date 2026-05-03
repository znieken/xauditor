"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import * as React from "react";
import {
  DEFAULT_PAGE_SIZE,
  PAGE_SIZE_OPTIONS,
  PageSizeScope,
  PageSizeSelector,
  readStoredPageSize,
} from "@/components/page-size-selector";
import { Button } from "@/components/ui/button";

export interface PaginationState {
  page: number; // 1-indexed
  pageSize: number;
  offset: number;
  setPage: (page: number) => void;
  setPageSize: (pageSize: number) => void;
}

export function usePagination(scope: PageSizeScope): PaginationState {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  // Initial size: URL wins over localStorage; default 25.
  const initialSize = React.useMemo(() => {
    const raw = searchParams?.get("pageSize");
    if (raw) {
      const parsed = Number.parseInt(raw, 10);
      if (
        PAGE_SIZE_OPTIONS.includes(parsed as (typeof PAGE_SIZE_OPTIONS)[number])
      ) {
        return parsed;
      }
    }
    return readStoredPageSize(scope, DEFAULT_PAGE_SIZE);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- first mount only
  }, []);

  const [pageSize, setPageSize] = React.useState<number>(initialSize);
  const [page, setPage] = React.useState<number>(() => {
    const raw = searchParams?.get("page");
    if (raw) {
      const parsed = Number.parseInt(raw, 10);
      if (Number.isFinite(parsed) && parsed >= 1) return parsed;
    }
    return 1;
  });

  // Mirror state into the URL so copy-paste reproduces the view.
  React.useEffect(() => {
    const params = new URLSearchParams(searchParams?.toString() ?? "");
    params.set("page", String(page));
    params.set("pageSize", String(pageSize));
    router.replace(`${pathname}?${params.toString()}`);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- derived from page & pageSize
  }, [page, pageSize]);

  return {
    page,
    pageSize,
    offset: (page - 1) * pageSize,
    setPage,
    setPageSize: (next) => {
      setPageSize(next);
      setPage(1); // new page size resets to first page
    },
  };
}

interface PaginationFooterProps {
  total: number;
  pagination: PaginationState;
  scope: PageSizeScope;
  label?: string;
}

export function PaginationFooter({
  total,
  pagination,
  scope,
  label = "items",
}: PaginationFooterProps) {
  const { page, pageSize, setPage, setPageSize } = pagination;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const from = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const to = Math.min(total, page * pageSize);

  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-t border-zinc-100 px-4 py-2 text-xs text-zinc-500 dark:border-zinc-800 dark:text-zinc-400">
      <div>
        Showing <span className="tabular-nums">{from}</span>–
        <span className="tabular-nums">{to}</span> of{" "}
        <span className="tabular-nums">{total}</span> {label}
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <PageSizeSelector
          scope={scope}
          value={pageSize}
          onChange={setPageSize}
        />
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setPage(Math.max(1, page - 1))}
            disabled={page <= 1}
            aria-label="Previous page"
          >
            <ChevronLeft className="h-4 w-4" aria-hidden />
          </Button>
          <span className="px-1 tabular-nums">
            {page} / {totalPages}
          </span>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setPage(Math.min(totalPages, page + 1))}
            disabled={page >= totalPages}
            aria-label="Next page"
          >
            <ChevronRight className="h-4 w-4" aria-hidden />
          </Button>
        </div>
      </div>
    </div>
  );
}
