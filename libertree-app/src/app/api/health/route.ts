import { verifyCatalogueReadiness } from "../../../lib/catalogue"
import { LibertreePathContractError } from "../../../lib/data-path"
import { authenticateInternalRequest, internalAuthFailure } from "../../../lib/internal-auth"

export const runtime = "nodejs"
export const dynamic = "force-dynamic"

const ready = (): Response => Response.json(
  { status: "ready", mode: "read-only", contract: "validated" },
  { headers: { "cache-control": "no-store" } },
)

const notReady = (): Response => Response.json(
  { status: "not-ready", mode: "read-only", contract: "invalid" },
  { status: 503, headers: { "cache-control": "no-store" } },
)

export async function GET(request: Request): Promise<Response> {
  const auth = authenticateInternalRequest(request)
  if (auth.kind !== "authorized") {
    const failure = internalAuthFailure(auth)
    return new Response(failure.body, failure)
  }
  try {
    verifyCatalogueReadiness()
    return ready()
  } catch (error) {
    if (error instanceof LibertreePathContractError) return notReady()
    throw error
  }
}
