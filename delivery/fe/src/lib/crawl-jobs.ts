import "server-only";

export type CrawlJobSummary = Readonly<{
  id: number;
  site_id: string;
  status: string;
  created_at: string;
}>;

const backendUrl = (): string => (process.env.BE_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");

// Server-side twin of the /api/jobs proxy the job table polls from the
// browser. The run screen needs the same list during render to build its
// "최근 실행함" queue, and waiting for a client round-trip would leave the
// first paint without one of its three entry points. Failure is not
// distinguished from "no jobs": the queue simply does not appear, while the
// job table below it still surfaces the connection error to the operator.
export async function getRecentJobs(limit = 50): Promise<readonly CrawlJobSummary[]> {
  try {
    const response = await fetch(`${backendUrl()}/jobs?limit=${limit}`, {
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    });
    if (!response.ok) return [];
    const payload = await response.json() as { jobs?: readonly CrawlJobSummary[] };
    return payload.jobs ?? [];
  } catch {
    return [];
  }
}
