import { NextResponse } from "next/server";
import { operatorHeaders } from "@/lib/backend-auth";
import { sameOriginJsonRequest } from "@/lib/same-origin";

type RouteContext = { params: Promise<{ runId: string }> };

const backendUrl = (): string => (process.env.BE_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");

export async function POST(request: Request, context: RouteContext): Promise<NextResponse> {
  if (!sameOriginJsonRequest(request)) {
    return NextResponse.json({ reason: "cross_origin", detail: "이 화면에서 보낸 JSON 요청만 받습니다." }, { status: 403 });
  }
  const { runId } = await context.params;
  if (!/^r[0-9]{4,12}$/.test(runId)) {
    return NextResponse.json({ reason: "bad_request", detail: "유효하지 않은 작업 ID입니다." }, { status: 400 });
  }
  try {
    const response = await fetch(`${backendUrl()}/collect/runs/${runId}/cancel`, {
      method: "POST",
      headers: operatorHeaders(true),
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
