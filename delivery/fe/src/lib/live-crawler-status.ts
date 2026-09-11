import "server-only";

export type Job = { id: number; status: string; saved_count: number; error: string | null; started_at: string | null; finished_at: string | null };
export type RuntimeRow = { site_id: string; latest_job: Job | null; last_result: Job | null; last_success: Job | null; active_counts: Record<string, number>; result_state: string };
export type Runtime = { available: boolean; measured_at: string | null; items: RuntimeRow[] };

export async function getLiveCrawlerStatus(): Promise<Runtime> {
  try {
    const base = (process.env.BE_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");
    const response = await fetch(`${base}/crawler-status`, { cache: "no-store", signal: AbortSignal.timeout(5000) });
    if (!response.ok) throw new Error("unavailable");
    const data = await response.json();
    if (!Array.isArray(data.items)) throw new Error("invalid response");
    return { available: true, measured_at: data.measured_at, items: data.items };
  } catch {
    return { available: false, measured_at: null, items: [] };
  }
}
