import { NextRequest, NextResponse } from "next/server";

const backend = () => (process.env.BE_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");

export async function GET(request: NextRequest) {
  try {
    const response = await fetch(`${backend()}/translation/quality?${request.nextUrl.searchParams}`, {
      cache: "no-store", signal: AbortSignal.timeout(8000),
    });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return NextResponse.json({ detail: "backend unavailable" }, { status: 503 });
  }
}
