export type InternalAuthResult =
  | { readonly kind: "authorized" }
  | { readonly kind: "misconfigured" }
  | { readonly kind: "unauthorized" };

interface InternalAuthFailure {
  readonly body: string;
  readonly headers: Readonly<Record<string, string>>;
  readonly status: 401 | 503;
}

const REALM = "Libertree Internal";
const ADMIN_USER = process.env.ADMIN_USER ?? "";
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD ?? "";

function credentialsConfigured(): boolean {
  return ADMIN_USER.length > 0 && ADMIN_PASSWORD.length > 0;
}

function decodeBasicCredentials(header: string): { readonly password: string; readonly user: string } | null {
  if (!header.startsWith("Basic ")) return null;

  try {
    const decoded = atob(header.slice(6));
    const separator = decoded.indexOf(":");
    if (separator <= 0) return null;

    return {
      user: decoded.slice(0, separator),
      password: decoded.slice(separator + 1),
    };
  } catch {
    return null;
  }
}

function safeEqual(left: string, right: string): boolean {
  if (left.length !== right.length) return false;

  let mismatch = 0;
  for (let index = 0; index < left.length; index += 1) {
    mismatch |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return mismatch === 0;
}

export function authenticateInternalRequest(request: Request): InternalAuthResult {
  if (!credentialsConfigured()) return { kind: "misconfigured" };

  const credentials = decodeBasicCredentials(request.headers.get("authorization") ?? "");
  if (!credentials) return { kind: "unauthorized" };

  if (
    safeEqual(credentials.user, ADMIN_USER) &&
    safeEqual(credentials.password, ADMIN_PASSWORD)
  ) {
    return { kind: "authorized" };
  }

  return { kind: "unauthorized" };
}

export function internalAuthFailure(result: Exclude<InternalAuthResult, { readonly kind: "authorized" }>): InternalAuthFailure {
  if (result.kind === "misconfigured") {
    return {
      status: 503,
      body: "Internal authentication is not configured",
      headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" },
    };
  }

  return {
    status: 401,
    body: "Authentication required",
    headers: {
      "content-type": "text/plain; charset=utf-8",
      "cache-control": "no-store",
      "www-authenticate": `Basic realm="${REALM}", charset="UTF-8"`,
    },
  };
}
