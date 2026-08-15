import { NextRequest, NextResponse } from "next/server";
import { operatorHeaders } from "@/lib/backend-auth";
import { parseScheduleLimit } from "@/lib/crawl-limit";

const backend = () => (process.env.BE_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");

// Mirrors the backend's own ScheduleIn bounds (be/app.py), so a malformed
// body is rejected here rather than forwarded and rejected there.
const MIN_INTERVAL_HOURS = 1;
const MAX_INTERVAL_HOURS = 8760;

type ScheduleRequest = {
  readonly interval_hours?: unknown;
  readonly mode?: unknown;
  readonly limit_n?: unknown;
  readonly enabled?: unknown;
};

export async function PUT(request: NextRequest, context: { params: Promise<{ siteId: string }> }) {
  const { siteId } = await context.params;
  if (!siteId || siteId.includes("/")) return NextResponse.json({ detail: "invalid site" }, { status: 422 });

  let body: ScheduleRequest;
  try {
    body = await request.json() as ScheduleRequest;
  } catch {
    return NextResponse.json({ detail: "요청 형식이 올바르지 않습니다." }, { status: 400 });
  }

  const intervalHours = typeof body.interval_hours === "number" ? body.interval_hours : Number(body.interval_hours);
  if (!Number.isInteger(intervalHours) || intervalHours < MIN_INTERVAL_HOURS || intervalHours > MAX_INTERVAL_HOURS) {
    return NextResponse.json(
      { detail: `수집 주기는 ${MIN_INTERVAL_HOURS}~${MAX_INTERVAL_HOURS}시간 사이의 정수여야 합니다.` },
      { status: 400 },
    );
  }

  const mode = body.mode === "full" || body.mode === "incremental" ? body.mode : null;
  if (mode === null) {
    return NextResponse.json({ detail: "수집 모드는 incremental 또는 full이어야 합니다." }, { status: 400 });
  }

  // Absence must never mean unbounded, and a schedule repeats unattended,
  // so unlike the run panel's one-off crawl, unbounded is not offered here
  // at all — parseScheduleLimit rejects both a blank/missing limit and the
  // explicit unbounded sentinel.
  const limitResult = parseScheduleLimit(body.limit_n);
  if (!limitResult.ok) {
    return NextResponse.json({ detail: limitResult.error }, { status: 400 });
  }

  if (typeof body.enabled !== "boolean") {
    return NextResponse.json({ detail: "활성 여부 값이 올바르지 않습니다." }, { status: 400 });
  }

  try {
    const response = await fetch(`${backend()}/schedules/${encodeURIComponent(siteId)}`, {
      method: "PUT",
      headers: operatorHeaders(true),
      body: JSON.stringify({ interval_hours: intervalHours, mode, limit_n: limitResult.limit, enabled: body.enabled }),
      cache: "no-store",
      signal: AbortSignal.timeout(8000),
    });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return NextResponse.json({ detail: "backend unavailable" }, { status: 503 });
  }
}
