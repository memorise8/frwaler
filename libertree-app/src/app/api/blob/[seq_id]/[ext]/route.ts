import { createReadStream, statSync } from "node:fs"
import { basename } from "node:path"
import { blobPathFor, getBlobAuthorization } from "../../../../../lib/catalogue"
import { authenticateInternalRequest, internalAuthFailure } from "../../../../../lib/internal-auth"

export const runtime = "nodejs"
export const dynamic = "force-dynamic"

const notFound = (): Response => new Response("Not found", { status: 404, headers: { "cache-control": "no-store", "content-type": "text/plain; charset=utf-8" } })
const parseSeqId = (value: string): number | null => /^\d{1,12}$/.test(value) && Number.isSafeInteger(Number(value)) ? Number(value) : null

export async function GET(request: Request, { params }: Readonly<{ params: Promise<{ readonly ext: string; readonly seq_id: string }> }>): Promise<Response> {
  const auth = authenticateInternalRequest(request)
  if (auth.kind !== "authorized") {
    const failure = internalAuthFailure(auth)
    return new Response(failure.body, failure)
  }
  const { ext, seq_id: rawSeqId } = await params
  const seqId = parseSeqId(rawSeqId)
  const extension = ext.toLowerCase()
  if (seqId === null || (extension !== "pdf" && extension !== "txt")) return notFound()
  const authorization = getBlobAuthorization(seqId, extension)
  if (authorization === null || !authorization.available) return notFound()
  const path = blobPathFor(seqId, extension)
  let size: number
  try {
    const stats = statSync(path)
    if (!stats.isFile()) return notFound()
    size = stats.size
  } catch {
    return notFound()
  }
  const stream = createReadStream(path)
  const body = new ReadableStream<Uint8Array>({
    start(controller): void {
      stream.on("data", (chunk: Buffer | string) => controller.enqueue(typeof chunk === "string" ? new TextEncoder().encode(chunk) : new Uint8Array(chunk.buffer, chunk.byteOffset, chunk.byteLength)))
      stream.on("end", () => controller.close())
      stream.on("error", (error: Error) => controller.error(error))
    },
    cancel(): void { stream.destroy() },
  })
  return new Response(body, { status: 200, headers: { "cache-control": "private, max-age=0, must-revalidate", "content-disposition": `inline; filename=\"${basename(path)}\"`, "content-length": String(size), "content-type": extension === "pdf" ? "application/pdf" : "text/plain; charset=utf-8" } })
}
