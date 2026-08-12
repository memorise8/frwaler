import { NextRequest, NextResponse } from "next/server";

const backend = () => (process.env.BE_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");
export async function POST(_request: NextRequest, { params }: { params: Promise<{ jobId: string; action: string }> }) {
  const { jobId, action } = await params;
  if (!/^\d+$/.test(jobId) || !new Set(["cancel", "retry"]).has(action)) return NextResponse.json({ detail: "invalid action" }, { status: 422 });
  try {
    const response = await fetch(`${backend()}/translation/jobs/${jobId}/${action}`, { method: "POST", cache: "no-store", signal: AbortSignal.timeout(8000) });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch { return NextResponse.json({ detail: "backend unavailable" }, { status: 503 }); }
}
