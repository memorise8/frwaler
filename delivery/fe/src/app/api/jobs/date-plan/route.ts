import { NextResponse } from "next/server";
import { operatorHeaders } from "@/lib/backend-auth";
import { describeJobSubmitFailure } from "@/lib/job-submit-error";

export async function POST(request: Request): Promise<NextResponse> {
  let body: unknown;
  try { body = await request.json(); }
  catch { return NextResponse.json({ error: "요청 형식이 올바르지 않습니다." }, { status: 400 }); }
  try {
    const backend = (process.env.BE_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");
    const response = await fetch(`${backend}/jobs/date-plan`, { method: "POST", headers: operatorHeaders(true),
      body: JSON.stringify(body), cache: "no-store", signal: AbortSignal.timeout(10000) });
    const result = await response.json().catch(() => ({}));
    return NextResponse.json(response.ok ? result : { error: describeJobSubmitFailure(response.status, result) }, { status: response.ok ? 201 : response.status });
  } catch { return NextResponse.json({ error: "범위 작업 서비스에 연결할 수 없습니다." }, { status: 503 }); }
}
