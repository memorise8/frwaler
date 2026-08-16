import { NextResponse } from "next/server";
import { operatorHeaders } from "@/lib/backend-auth";
import { classifyBulkOutcome, parseBulkRunRequest, summarizeBulkRun, type BulkOutcome, type BulkRunRequest } from "@/lib/bulk-run";

const backendUrl = (): string => (process.env.BE_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");

// Enqueuing is a database insert, so the fan-out is fast even at fleet scale --
// what takes hours afterwards is the single serial worker draining the queue,
// not this request. The cap keeps a 804-site sweep from opening 804 sockets at
// once against the backend.
const CONCURRENCY = 5;

const enqueueOne = async (siteId: string, request: BulkRunRequest): Promise<BulkOutcome> => {
  try {
    const response = await fetch(`${backendUrl()}/jobs`, {
      method: "POST",
      headers: operatorHeaders(true),
      body: JSON.stringify({ site_id: siteId, mode: request.mode, limit_n: request.limit }),
      cache: "no-store",
      signal: AbortSignal.timeout(15_000),
    });
    const payload = await response.json().catch(() => ({}));
    return classifyBulkOutcome(siteId, response.status, payload);
  } catch (error) {
    return { siteId, kind: "failed", reason: error instanceof Error ? error.message : String(error) };
  }
};

export async function POST(request: Request): Promise<NextResponse> {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "요청 본문을 읽을 수 없습니다." }, { status: 400 });
  }
  const parsed = parseBulkRunRequest(body);
  if (!parsed.ok) return NextResponse.json({ error: parsed.error }, { status: 400 });

  const { siteIds } = parsed.value;
  const outcomes: BulkOutcome[] = new Array(siteIds.length);
  let cursor = 0;
  const worker = async (): Promise<void> => {
    while (cursor < siteIds.length) {
      const index = cursor;
      cursor += 1;
      outcomes[index] = await enqueueOne(siteIds[index]!, parsed.value);
    }
  };
  await Promise.all(Array.from({ length: Math.min(CONCURRENCY, siteIds.length) }, worker));

  const summary = summarizeBulkRun(outcomes);
  return NextResponse.json({
    summary,
    // Only the outcomes an operator has to act on are returned. Echoing 800
    // successful enqueues back would bury the handful that need attention.
    problems: outcomes.filter((item) => item.kind !== "queued").slice(0, 50),
  });
}
