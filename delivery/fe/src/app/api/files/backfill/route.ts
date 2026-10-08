import { NextResponse } from "next/server";
import { operatorHeaders } from "@/lib/backend-auth";

export async function POST(request: Request) {
  let body: unknown;
  try { body = await request.json(); } catch { return NextResponse.json({detail: "invalid JSON"}, {status:400}); }
  try {
    const root = (process.env.BE_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");
    const response = await fetch(`${root}/files/backfill`, {method:"POST", headers:operatorHeaders(true),
      body:JSON.stringify(body), cache:"no-store", signal:AbortSignal.timeout(15000)});
    return NextResponse.json(await response.json(), {status:response.status});
  } catch { return NextResponse.json({detail:"파일 처리 서비스에 연결할 수 없습니다."}, {status:503}); }
}
