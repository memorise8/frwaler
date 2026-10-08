import { NextResponse } from "next/server";
import { operatorHeaders } from "@/lib/backend-auth";

export async function GET(_request: Request, context: { params: Promise<{siteId: string}> }) {
  const { siteId } = await context.params;
  if (!/^[A-Za-z0-9._-]+$/.test(siteId)) return NextResponse.json({ error: "invalid site" }, { status: 422 });
  try {
    const root = (process.env.BE_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");
    const response = await fetch(`${root}/sites/${encodeURIComponent(siteId)}/capabilities`, {
      headers: operatorHeaders(), cache: "no-store", signal: AbortSignal.timeout(10000),
    });
    return NextResponse.json(await response.json(), {status: response.status});
  } catch {
    return NextResponse.json({error: "수집기 지원 정보를 확인할 수 없습니다."}, {status: 503});
  }
}
