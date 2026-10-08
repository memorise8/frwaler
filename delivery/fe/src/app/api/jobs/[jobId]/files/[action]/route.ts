import { NextResponse } from "next/server";
import { operatorHeaders } from "@/lib/backend-auth";

export async function POST(_request: Request, { params }: { params: Promise<{ jobId: string; action: string }> }) {
  const { jobId, action } = await params;
  if (!/^\d+$/.test(jobId) || !["retry", "cancel"].includes(action)) {
    return NextResponse.json({ error: "잘못된 작업 요청입니다." }, { status: 422 });
  }
  try {
    const base = (process.env.BE_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");
    const response = await fetch(`${base}/jobs/${jobId}/files/${action}`, {
      method: "POST", headers: operatorHeaders(true), cache: "no-store", signal: AbortSignal.timeout(10000),
    });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return NextResponse.json({ error: "파일 작업 서버에 연결할 수 없습니다." }, { status: 503 });
  }
}
