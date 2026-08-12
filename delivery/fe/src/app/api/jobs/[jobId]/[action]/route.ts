import { NextResponse } from "next/server";
import { operatorHeaders } from "@/lib/backend-auth";

type RouteContext = { params: Promise<{ jobId: string; action: string }> };

const backendUrl = (): string => (process.env.BE_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");

export async function POST(_request: Request, context: RouteContext): Promise<NextResponse> {
  const { jobId, action } = await context.params;
  if (!/^\d+$/.test(jobId) || !["cancel", "retry"].includes(action)) {
    return NextResponse.json({ error: "유효하지 않은 작업 요청입니다." }, { status: 400 });
  }
  try {
    const response = await fetch(`${backendUrl()}/jobs/${jobId}/${action}`, {
      method: "POST",
      headers: operatorHeaders(true),
      body: action === "retry" ? JSON.stringify({ requested_by: "delivery-fe" }) : undefined,
      cache: "no-store",
      signal: AbortSignal.timeout(10_000),
    });
    const result = await response.json().catch(() => ({})) as Record<string, unknown>;
    return NextResponse.json(result, { status: response.status });
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    return NextResponse.json({ error: "작업 BE에 연결할 수 없습니다.", detail }, { status: 503 });
  }
}
