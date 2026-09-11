import { NextResponse } from "next/server";

type JobRequest = {
  readonly siteId?: unknown;
  readonly mode?: unknown;
  readonly limit?: unknown;
};

const backendUrl = (): string => (process.env.BE_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");

export async function POST(request: Request): Promise<NextResponse> {
  let body: JobRequest;
  try {
    body = await request.json() as JobRequest;
  } catch {
    return NextResponse.json({ error: "요청 형식이 올바르지 않습니다." }, { status: 400 });
  }
  const siteId = typeof body.siteId === "string" ? body.siteId.trim() : "";
  const mode = body.mode === "full" ? "full" : "incremental";
  const parsedLimit = typeof body.limit === "number" ? body.limit : Number(body.limit);
  const limit = Number.isInteger(parsedLimit) && parsedLimit > 0 ? parsedLimit : null;
  if (!/^[a-z0-9][a-z0-9_-]{1,127}$/.test(siteId)) {
    return NextResponse.json({ error: "유효한 크롤러 ID가 필요합니다." }, { status: 400 });
  }
  try {
    const response = await fetch(`${backendUrl()}/jobs`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ site_id: siteId, mode, limit_n: limit, requested_by: "delivery-fe" }),
      cache: "no-store",
      signal: AbortSignal.timeout(10_000),
    });
    const result = await response.json().catch(() => ({})) as Record<string, unknown>;
    if (!response.ok) {
      const detail = typeof result.detail === "string" ? result.detail : `BE 응답 오류 (${response.status})`;
      return NextResponse.json({ error: detail }, { status: response.status });
    }
    return NextResponse.json({ jobId: result.id, status: "queued" }, { status: 201 });
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    return NextResponse.json({ error: "수집 BE에 연결할 수 없습니다.", detail }, { status: 503 });
  }
}
