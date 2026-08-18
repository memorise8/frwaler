import { NextResponse } from "next/server";

const backendUrl = (): string => (process.env.BE_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");

export async function GET(): Promise<NextResponse> {
  try {
    const response = await fetch(`${backendUrl()}/progress`, {
      cache: "no-store",
      signal: AbortSignal.timeout(10_000),
    });
    const result = await response.json().catch(() => ({})) as Record<string, unknown>;
    return NextResponse.json(result, { status: response.status });
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    return NextResponse.json({ error: "백필 진행 상황을 불러올 수 없습니다.", detail }, { status: 503 });
  }
}
