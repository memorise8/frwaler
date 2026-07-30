// Next.js 16 Proxy (formerly middleware) — HTTP Basic Auth gate for the admin UI.
//
// Behaviour:
//   - Missing or empty credentials fail closed with HTTP 503.
//   - Every request matching the matcher below requires Basic Auth.
//   - Constant-time comparison is used to avoid trivial timing oracles.
//
// Matcher excludes Next.js internals and static files so the 401 doesn't break CSS/JS.
// See: node_modules/next/dist/docs/01-app/03-api-reference/03-file-conventions/proxy.md

import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';
import { authenticateInternalRequest, internalAuthFailure } from '@/lib/internal-auth';

export function proxy(req: NextRequest) {
  const result = authenticateInternalRequest(req);
  if (result.kind === 'authorized') {
    return NextResponse.next();
  }

  const failure = internalAuthFailure(result);
  return new NextResponse(failure.body, {
    status: failure.status,
    headers: failure.headers,
  });
}

export const config = {
  // Match every path except Next.js internals and common static assets.
  // Files with an extension (e.g. `.png`, `.css`, `.js`) are let through so the
  // authentication response doesn't prevent the browser from loading its UI assets.
  matcher: ['/((?!_next/|favicon.ico|robots.txt|sitemap.xml|.*\\..*).*)'],
};
