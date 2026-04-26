// Next.js 16 Proxy (formerly middleware) — HTTP Basic Auth gate for the admin UI.
//
// Behaviour:
//   - If ADMIN_USER or ADMIN_PASSWORD is missing/empty, auth is disabled
//     (explicit opt-out for local dev). A warning is printed to stderr at import time.
//   - Otherwise every request matching the matcher below prompts for Basic Auth.
//   - Constant-time comparison is used to avoid trivial timing oracles.
//
// Matcher excludes Next.js internals and static files so the 401 doesn't break CSS/JS.
// See: node_modules/next/dist/docs/01-app/03-api-reference/03-file-conventions/proxy.md

import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

const REALM = 'Finolaw Admin';

const ADMIN_USER = process.env.ADMIN_USER ?? '';
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD ?? '';
const AUTH_ENABLED = ADMIN_USER.length > 0 && ADMIN_PASSWORD.length > 0;

if (!AUTH_ENABLED) {
  // Single stderr line at module load so operators see it in docker logs.
  console.warn(
    '[proxy] ADMIN_USER / ADMIN_PASSWORD not set — admin auth is DISABLED. Do not expose this server to the public internet.',
  );
}

// Constant-time-ish string comparison. Not cryptographically perfect (JS strings
// are variable-length) but defeats the most naive timing attacks for short creds.
function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let mismatch = 0;
  for (let i = 0; i < a.length; i++) {
    mismatch |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return mismatch === 0;
}

function unauthorized(): NextResponse {
  return new NextResponse('Authentication required', {
    status: 401,
    headers: {
      'WWW-Authenticate': `Basic realm="${REALM}", charset="UTF-8"`,
    },
  });
}

export function proxy(req: NextRequest) {
  if (!AUTH_ENABLED) return NextResponse.next();

  const header = req.headers.get('authorization') ?? '';
  if (!header.startsWith('Basic ')) return unauthorized();

  let decoded = '';
  try {
    decoded = Buffer.from(header.slice(6), 'base64').toString('utf-8');
  } catch {
    return unauthorized();
  }

  const idx = decoded.indexOf(':');
  if (idx <= 0) return unauthorized();

  const u = decoded.slice(0, idx);
  const p = decoded.slice(idx + 1);

  if (safeEqual(u, ADMIN_USER) && safeEqual(p, ADMIN_PASSWORD)) {
    return NextResponse.next();
  }
  return unauthorized();
}

export const config = {
  // Match every path except Next.js internals and common static assets.
  // Files with an extension (e.g. `.png`, `.css`, `.js`) are let through so the
  // login prompt doesn't break the UI that renders behind it.
  matcher: ['/((?!_next/|favicon.ico|robots.txt|sitemap.xml|.*\\..*).*)'],
};
