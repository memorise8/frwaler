import { NextResponse } from "next/server";
import { operatorHeaders } from "@/lib/backend-auth";
import { sameOriginJsonRequest } from "@/lib/same-origin";

type RunRequest = { readonly siteId?: unknown; readonly mode?: unknown; readonly limit?: unknown };

const backendUrl = (): string => (process.env.BE_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");

// 실행기 요청을 BE /collect/runs 로 넘긴다. 409 본문({reason, detail})은 화면이
// 거절 사유를 그대로 보여 줄 수 있도록 손대지 않고 돌려준다.
export async function POST(request: Request): Promise<NextResponse> {
  if (!sameOriginJsonRequest(request)) {
    return NextResponse.json({ reason: "cross_origin", detail: "이 화면에서 보낸 JSON 요청만 받습니다." }, { status: 403 });
  }
  let body: RunRequest;
  try {
    body = await request.json() as RunRequest;
  } catch {
    return NextResponse.json({ reason: "bad_request", detail: "요청 형식이 올바르지 않습니다." }, { status: 400 });
  }
  const siteId = typeof body.siteId === "string" ? body.siteId.trim() : "";
  if (!/^[0-9A-Za-z._-]{1,120}$/.test(siteId)) {
    return NextResponse.json({ reason: "bad_request", detail: "유효한 사이트 ID가 필요합니다." }, { status: 400 });
  }
  if (body.mode !== "trial" && body.mode !== "resume") {
    return NextResponse.json({ reason: "bad_request", detail: "지원하지 않는 수집 모드입니다." }, { status: 400 });
  }
  if (body.limit !== undefined && (typeof body.limit !== "number" || !Number.isInteger(body.limit) || body.limit < 1 || body.limit > 100_000)) {
    return NextResponse.json({ reason: "bad_limit", detail: "건수는 1~100000 사이 정수여야 합니다." }, { status: 400 });
  }
  if (body.mode === "resume" && body.limit !== undefined) {
    return NextResponse.json({ reason: "bad_limit", detail: "이어받기에는 건수를 지정할 수 없습니다." }, { status: 400 });
  }
  try {
    const response = await fetch(`${backendUrl()}/collect/runs`, {
      method: "POST",
      headers: operatorHeaders(true),
      body: JSON.stringify({ site_id: siteId, mode: body.mode, ...(body.limit !== undefined ? { limit: body.limit } : {}) }),
      cache: "no-store",
      signal: AbortSignal.timeout(20_000),
    });
    const result = await response.json().catch(() => ({})) as Record<string, unknown>;
    return NextResponse.json(result, { status: response.status });
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    return NextResponse.json({ reason: "be_unreachable", detail }, { status: 503 });
  }
}
