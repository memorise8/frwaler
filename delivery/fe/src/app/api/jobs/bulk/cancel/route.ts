import { NextResponse } from "next/server";
import { operatorHeaders } from "@/lib/backend-auth";
import { parseBulkCancelRequest, type CancelSummary } from "@/lib/bulk-run";

const backendUrl = (): string => (process.env.BE_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");

// GET /jobs caps limit at 200 (delivery/be/app.py), so a fleet-sized queue has
// to be drained in rounds. The round guard bounds the work even if something
// keeps re-queueing underneath us -- a schedule firing mid-cancel, say -- so
// this handler cannot spin.
const PAGE = 200;
const MAX_ROUNDS = 12;
const CONCURRENCY = 5;

type Job = { id: number; status: string; requested_by?: string | null };

const listByStatus = async (status: string): Promise<readonly Job[]> => {
  const response = await fetch(`${backendUrl()}/jobs?status=${status}&limit=${PAGE}`, {
    cache: "no-store",
    signal: AbortSignal.timeout(10_000),
  });
  if (!response.ok) return [];
  const payload = await response.json() as { jobs?: readonly Job[] };
  return payload.jobs ?? [];
};

// A schedule attributes its jobs to 'scheduler' (delivery/worker/schedules.py);
// anything a person started through the console is 'customer-operator'. Only
// the whole-queue scope can reach the former, and the operator is told when it
// did.
const isScheduled = (job: Job): boolean => job.requested_by === "scheduler";

const cancelJobs = async (jobs: readonly Job[], summary: {
  cancelled: number; requested: number; alreadyDone: number; failed: number; scheduled: number;
}, countScheduled: boolean): Promise<void> => {
  let cursor = 0;
  const worker = async (): Promise<void> => {
    while (cursor < jobs.length) {
      const job = jobs[cursor]!;
      cursor += 1;
      try {
        const response = await fetch(`${backendUrl()}/jobs/${job.id}/cancel`, {
          method: "POST",
          headers: operatorHeaders(true),
          cache: "no-store",
          signal: AbortSignal.timeout(10_000),
        });
        // 409 means the job left queued/running before this landed -- it
        // finished, failed, or was already cancelled. Expected for a bulk run
        // whose ids go stale as the worker drains them, so not a failure.
        if (response.status === 409) { summary.alreadyDone += 1; continue; }
        if (!response.ok) { summary.failed += 1; continue; }
        // cancel_job returns the updated row: a queued job settles straight
        // into "cancelled", a running one into "cancelling" and keeps
        // collecting until its next checkpoint.
        const row = await response.json().catch(() => ({})) as { status?: string };
        if (row.status === "cancelled") summary.cancelled += 1;
        else summary.requested += 1;
        if (countScheduled && isScheduled(job)) summary.scheduled += 1;
      } catch {
        summary.failed += 1;
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(CONCURRENCY, jobs.length) }, worker));
};

export async function POST(request: Request): Promise<NextResponse> {
  const body = await request.json().catch(() => ({}));
  const parsed = parseBulkCancelRequest(body);
  if (!parsed.ok) return NextResponse.json({ error: parsed.error }, { status: 400 });

  const summary = { cancelled: 0, requested: 0, alreadyDone: 0, failed: 0, scheduled: 0 };
  try {
    if (parsed.value.scope === "jobs") {
      // Exactly the ids handed in. No listing, so nothing else in the queue can
      // be swept up by a bulk run stopping its own work.
      await cancelJobs(parsed.value.jobIds.map((id) => ({ id, status: "unknown" })), summary, false);
    } else {
      for (let round = 0; round < MAX_ROUNDS; round += 1) {
        const pending = [...await listByStatus("queued"), ...await listByStatus("running")];
        if (pending.length === 0) break;
        await cancelJobs(pending, summary, true);
        // A running job stays listed as "cancelling", not "running", so it will
        // not reappear in the next round and cannot loop.
        if (pending.length < PAGE) break;
      }
    }
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    return NextResponse.json({ error: "작업 BE에 연결할 수 없습니다.", detail, summary }, { status: 503 });
  }
  return NextResponse.json({ summary: summary as CancelSummary, scope: parsed.value.scope });
}
