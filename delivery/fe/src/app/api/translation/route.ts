import { NextRequest, NextResponse } from "next/server";

const backend = () => (process.env.BE_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");
export async function GET(request: NextRequest) {
  try {
    const response = await fetch(`${backend()}/translation/jobs?${request.nextUrl.searchParams}`, { cache: "no-store", signal: AbortSignal.timeout(8000) });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch { return NextResponse.json({ detail: "backend unavailable" }, { status: 503 }); }
}
export async function POST(request: NextRequest) {
  const action = request.nextUrl.searchParams.get("action");
  if (!new Set(["preview", "enqueue"]).has(action ?? "")) return NextResponse.json({ detail: "invalid action" }, { status: 422 });
  try {
    const response = await fetch(`${backend()}/translation/${action === "enqueue" ? "jobs" : "preview"}`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(await request.json()),
      cache: "no-store", signal: AbortSignal.timeout(8000),
    });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch { return NextResponse.json({ detail: "backend unavailable" }, { status: 503 }); }
}
