import { NextResponse } from "next/server"
import type { NextRequest } from "next/server"
import { authenticateInternalRequest, internalAuthFailure } from "./lib/internal-auth"

export function proxy(request: NextRequest): NextResponse {
  const result = authenticateInternalRequest(request)
  if (result.kind === "authorized") return NextResponse.next()
  const failure = internalAuthFailure(result)
  return new NextResponse(failure.body, { status: failure.status, headers: failure.headers })
}

export const config = {
  matcher: ["/((?!_next/|favicon.ico|robots.txt|sitemap.xml|.*\\..*).*)"],
}
