import { NextResponse } from "next/server";
import { operatorHeaders } from "@/lib/backend-auth";
import type { CancelSummary } from "@/lib/bulk-run";

const backendUrl = (): string => (process.env.BE_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");

// GET /jobs caps limit at 200 (delivery/be/app.py), so a fleet-sized queue has
// to be drained in rounds. The round guard bounds the work even if something
// keeps re-queueing underneath us -- a schedule firing mid-cancel, say -- so
// this handler cannot spin.
const PAGE = 200;
const MAX_ROUNDS = 12;
const CONCURRENCY = 5;

type Job = { id: number; status: string };

const listByStatus = async (status: string): Promise<readonly Job[]> => {
  const response = await fetch(`${backendUrl()}/jobs?status=${status}&limit=${PAGE}`, {
    cache: "no-store",
    signal: AbortSignal.timeout(10_000),
  });
  if (!response.ok) return [];
  const payload = await response.json() as { jobs?: readonly Job[] };
  return payload.jobs ?? [];
};

export async function POST(): Promise<NextResponse> {
  const summary = { cancelled: 0, requested: 0, failed: 0 };
  try {
    for (let round = 0; round < MAX_ROUNDS; round += 1) {
      const pending = [...await listByStatus("queued"), ...await listByStatus("running")];
      if (pending.length === 0) break;

      let cursor = 0;
      const worker = async (): Promise<void> => {
        while (cursor < pending.length) {
          const job = pending[cursor]!;
          cursor += 1;
          try {
            const response = await fetch(`${backendUrl()}/jobs/${job.id}/cancel`, {
              method: "POST",
              headers: operatorHeaders(true),
              cache: "no-store",
              signal: AbortSignal.timeout(10_000),
            });
            if (!response.ok) {
              summary.failed += 1;
              continue;
            }
            // cancel_job returns the updated row: a queued job settles straight
            // into "cancelled", a running one into "cancelling" and keeps
            // collecting until its next checkpoint.
            const row = await response.json().catch(() => ({})) as { status?: string };
            if (row.status === "cancelled") summary.cancelled += 1;
            else summary.requested += 1;
          } catch {
            summary.failed += 1;
          }
        }
      };
      await Promise.all(Array.from({ length: Math.min(CONCURRENCY, pending.length) }, worker));

      // A running job stays listed as "cancelling", not "running", so it will
      // not reappear in the next round and cannot loop.
      if (pending.length < PAGE) break;
    }
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    return NextResponse.json({ error: "작업 BE에 연결할 수 없습니다.", detail, summary }, { status: 503 });
  }
  return NextResponse.json({ summary: summary as CancelSummary });
}
