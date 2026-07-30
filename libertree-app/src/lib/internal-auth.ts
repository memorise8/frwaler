export type InternalAuthResult =
  | { readonly kind: "authorized" }
  | { readonly kind: "misconfigured" }
  | { readonly kind: "unauthorized" }

type InternalAuthFailure = {
  readonly body: string
  readonly headers: Readonly<Record<string, string>>
  readonly status: 401 | 503
}

const REALM = "Libertree Internal"

const credentialsConfigured = (): boolean =>
  Boolean(process.env["ADMIN_USER"] && process.env["ADMIN_PASSWORD"])

const dataContractConfigured = (): boolean =>
  process.env["LIBERTREE_APP_MODE"] === "read-only"
  && Boolean(process.env["LIBERTREE_DB_PATH"])
  && Boolean(process.env["LIBERTREE_BLOB_ROOT"])

const decodeBasicCredentials = (header: string): { readonly password: string; readonly user: string } | null => {
  if (!header.startsWith("Basic ")) return null
  try {
    const decoded = atob(header.slice(6))
    const separator = decoded.indexOf(":")
    if (separator <= 0) return null
    return { user: decoded.slice(0, separator), password: decoded.slice(separator + 1) }
  } catch {
    return null
  }
}

const safeEqual = (left: string, right: string): boolean => {
  if (left.length !== right.length) return false
  let mismatch = 0
  for (let index = 0; index < left.length; index += 1) {
    mismatch |= left.charCodeAt(index) ^ right.charCodeAt(index)
  }
  return mismatch === 0
}

export const authenticateInternalRequest = (request: Request): InternalAuthResult => {
  if (!credentialsConfigured() || !dataContractConfigured()) return { kind: "misconfigured" }
  const credentials = decodeBasicCredentials(request.headers.get("authorization") ?? "")
  if (!credentials) return { kind: "unauthorized" }
  return safeEqual(credentials.user, process.env["ADMIN_USER"] ?? "")
    && safeEqual(credentials.password, process.env["ADMIN_PASSWORD"] ?? "")
    ? { kind: "authorized" }
    : { kind: "unauthorized" }
}

export const internalAuthFailure = (
  result: Exclude<InternalAuthResult, { readonly kind: "authorized" }>,
): InternalAuthFailure => result.kind === "misconfigured"
  ? {
      status: 503,
      body: "Internal catalogue is not configured",
      headers: { "cache-control": "no-store", "content-type": "text/plain; charset=utf-8" },
    }
  : {
      status: 401,
      body: "Authentication required",
      headers: {
        "cache-control": "no-store",
        "content-type": "text/plain; charset=utf-8",
        "www-authenticate": `Basic realm="${REALM}", charset="UTF-8"`,
      },
    }
