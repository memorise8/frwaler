import { NextRequest, NextResponse } from "next/server";
import { operatorHeaders } from "@/lib/backend-auth";

const backend = () => (process.env.BE_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");

export async function PUT(request: NextRequest, context: { params: Promise<{ siteId: string }> }) {
  const { siteId } = await context.params;
  if (!siteId || siteId.includes("/")) return NextResponse.json({ detail: "invalid site" }, { status: 422 });
  try {
    const response = await fetch(`${backend()}/schedules/${encodeURIComponent(siteId)}`, {
      method: "PUT", headers: operatorHeaders(true),
      body: JSON.stringify(await request.json()), cache: "no-store", signal: AbortSignal.timeout(8000),
    });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return NextResponse.json({ detail: "backend unavailable" }, { status: 503 });
  }
}
