"use client";

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";

interface Props {
  docId: number;
}

export function RegenerateButton({ docId }: Props) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const onClick = async () => {
    if (busy) return;
    setError(null);
    setBusy(true);
    try {
      const res = await fetch("/api/admin/summarize", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ docId }),
      });
      const json = (await res.json()) as { ok: boolean; error?: string };
      if (!res.ok || !json.ok) {
        throw new Error(json.error || `HTTP ${res.status}`);
      }
      // Re-fetch the server component data so the new summary shows up.
      startTransition(() => router.refresh());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="inline-flex items-center gap-2">
      <button
        type="button"
        onClick={onClick}
        disabled={busy || pending}
        className="px-2.5 py-1 text-xs rounded-lg border border-blue-300 dark:border-blue-700 bg-blue-50 dark:bg-blue-950/40 text-blue-700 dark:text-blue-200 hover:bg-blue-100 dark:hover:bg-blue-950/60 disabled:opacity-60 disabled:cursor-progress"
      >
        {busy || pending ? "요약 중…" : "재요약"}
      </button>
      {error && (
        <span className="text-xs text-red-600 dark:text-red-400 max-w-[16rem] truncate" title={error}>
          {error}
        </span>
      )}
    </div>
  );
}
