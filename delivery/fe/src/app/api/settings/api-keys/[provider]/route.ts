import { NextResponse } from "next/server";
import { operatorHeaders } from "@/lib/backend-auth";
import { sameOriginJsonRequest } from "@/lib/same-origin";
import { API_KEY_MAX_BODY, API_KEY_MAX_LENGTH, API_KEY_PROVIDER_RE } from "@/lib/api-keys";

const backendUrl = (): string => (process.env.BE_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");

// content-length 가 없어도(chunked) 한도를 넘는 순간 읽기를 멈춘다. 바이트로 센다.
const readBounded = async (request: Request): Promise<string | null> => {
  if (!request.body) return "";
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > API_KEY_MAX_BODY) {
        await reader.cancel().catch(() => undefined);
        return null;
      }
      chunks.push(value);
    }
  } catch {
    return null;
  }
  return new TextDecoder().decode(Buffer.concat(chunks));
};

// 「시험 후 저장」을 BE /settings/api-keys/{provider} 로 넘긴다. 키는 그대로 BE 로만 가고,
// 이 경로의 어떤 응답에도 담기지 않는다(BE 응답도 결과·사유만).
export async function POST(request: Request, context: { params: Promise<{ provider: string }> }): Promise<NextResponse> {
  if (!sameOriginJsonRequest(request)) {
    return NextResponse.json({ reason: "cross_origin", detail: "이 화면에서 보낸 JSON 요청만 받습니다." }, { status: 403 });
  }
  const { provider } = await context.params;
  if (!API_KEY_PROVIDER_RE.test(provider)) {
    return NextResponse.json({ reason: "unknown_provider", detail: "알 수 없는 공급자입니다." }, { status: 404 });
  }
  const declared = Number(request.headers.get("content-length") ?? "0");
  if (declared > API_KEY_MAX_BODY) {
    return NextResponse.json({ reason: "too_large", detail: "요청이 너무 큽니다." }, { status: 413 });
  }
  const text = await readBounded(request);
  if (text === null) {
    return NextResponse.json({ reason: "too_large", detail: "요청이 너무 큽니다." }, { status: 413 });
  }
  let key: unknown;
  try {
    key = (JSON.parse(text) as { key?: unknown }).key;
  } catch {
    key = undefined;
  }
  if (typeof key !== "string" || !key.trim() || key.length > API_KEY_MAX_LENGTH) {
    return NextResponse.json({ reason: "invalid_format", detail: "키 형식이 맞지 않습니다." }, { status: 422 });
  }
  try {
    const response = await fetch(`${backendUrl()}/settings/api-keys/${provider}`, {
      method: "POST",
      headers: operatorHeaders(true),
      body: JSON.stringify({ key }),
      cache: "no-store",
      // BE 는 공급자에 시험 요청(먼저 robots.txt)을 하고 30초 안에 답한다.
      signal: AbortSignal.timeout(90_000),
    });
    const result = await response.json().catch(() => ({})) as Record<string, unknown>;
    const { reason, detail, result: outcome, at } = result;
    return NextResponse.json({ reason, detail, result: outcome, at }, { status: response.status });
  } catch {
    return NextResponse.json({ reason: "connect_failed", detail: "서버에 연결하지 못했습니다." }, { status: 503 });
  }
}
