import { NextRequest, NextResponse } from "next/server";
import { operatorHeaders } from "@/lib/backend-auth";

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
    const submitted=await request.json() as Record<string,unknown>;
    const payload={...submitted,provider:"internal",
      model_version:process.env.TRANSLATION_INTERNAL_MODEL??"qwen3-30b-a3b-instruct-2507",
      prompt_version:"title-summary-ko-v1"};
    const response = await fetch(`${backend()}/translation/${action === "enqueue" ? "jobs" : "preview"}`, {
      method: "POST", headers: operatorHeaders(true), body: JSON.stringify(payload),
      cache: "no-store", signal: AbortSignal.timeout(8000),
    });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch { return NextResponse.json({ detail: "backend unavailable" }, { status: 503 }); }
}
