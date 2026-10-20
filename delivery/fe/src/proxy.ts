import { NextResponse, type NextRequest } from "next/server";

// HTTP Basic auth in front of every page and /api/* route. The operator token
// is attached server-side by the API proxies, so without this anyone who can
// reach the FE could start or cancel collection runs.
//
// Credentials come from FE_BASIC_USER / FE_BASIC_PASS at request time (never
// inlined at build). If either is missing or empty every request is refused:
// fail closed rather than serve an open console.

export const REALM_HEADER = 'Basic realm="Dashboard Admin Area", charset="UTF-8"';

const encoder = new TextEncoder();

// Constant-time over the longer input: no early exit on length or content.
export const constantTimeEqual = (given: string, expected: string): boolean => {
  const a = encoder.encode(given);
  const b = encoder.encode(expected);
  const length = Math.max(a.length, b.length);
  let diff = a.length ^ b.length;
  for (let i = 0; i < length; i += 1) diff |= (a[i] ?? 0) ^ (b[i] ?? 0);
  return diff === 0;
};

const decodeBasic = (header: string | null): { user: string; pass: string } | null => {
  const match = /^Basic\s+([A-Za-z0-9+/=]+)\s*$/i.exec(header ?? "");
  if (!match) return null;
  try {
    const binary = atob(match[1]!);
    const text = new TextDecoder().decode(Uint8Array.from(binary, (char) => char.charCodeAt(0)));
    const colon = text.indexOf(":");
    return colon < 0 ? null : { user: text.slice(0, colon), pass: text.slice(colon + 1) };
  } catch {
    return null;
  }
};

// Exempt: liveness probe and immutable build assets only.
const isExempt = (request: NextRequest): boolean => {
  const path = request.nextUrl.pathname;
  if (path === "/healthz") return request.method === "GET";
  return path.startsWith("/_next/static/") || path === "/favicon.ico";
};

const unauthorized = (): NextResponse =>
  new NextResponse(null, { status: 401, headers: { "WWW-Authenticate": REALM_HEADER, "cache-control": "no-store" } });

export function proxy(request: NextRequest): NextResponse {
  if (isExempt(request)) return NextResponse.next();
  // Read through a computed key so no bundler step can replace it with a build-time value.
  const env = process.env;
  const user = env["FE_BASIC_" + "USER"] ?? "";
  const pass = env["FE_BASIC_" + "PASS"] ?? "";
  if (!user || !pass) return new NextResponse(null, { status: 503, headers: { "cache-control": "no-store" } });
  const given = decodeBasic(request.headers.get("authorization"));
  if (!given) return unauthorized();
  const userOk = constantTimeEqual(given.user, user);
  const passOk = constantTimeEqual(given.pass, pass);
  return userOk && passOk ? NextResponse.next() : unauthorized();
}

export const config = {
  // Everything except build assets and the favicon; /healthz is matched and
  // exempted above (GET only). /api is deliberately covered.
  matcher: ["/((?!_next/static/|favicon\\.ico$).*)"],
};
