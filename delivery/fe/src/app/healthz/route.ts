// Liveness probe for the container. Exempt from Basic auth (src/proxy.ts), so
// it must never return data: a fixed "ok" and nothing else.
export function GET(): Response {
  return new Response("ok", { status: 200, headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" } });
}
