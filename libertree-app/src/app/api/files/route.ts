import { readdirSync, realpathSync, statSync } from "node:fs"
import { join, sep } from "node:path"
import { loadLibertreeReadOnlyDataPaths } from "../../../lib/data-path"
import { authenticateInternalRequest, internalAuthFailure } from "../../../lib/internal-auth"

export const runtime = "nodejs"
export const dynamic = "force-dynamic"

const LIMIT = 2000

type DirEntry = { readonly type: "dir"; readonly name: string; readonly relPath: string }
type FileEntry = { readonly type: "file"; readonly name: string; readonly size: number; readonly ext: string; readonly seqId: number | null; readonly blobHref: string | null }
type Entry = DirEntry | FileEntry

const jsonResponse = (body: unknown, status: number): Response =>
  new Response(JSON.stringify(body), { status, headers: { "cache-control": "no-store", "content-type": "application/json; charset=utf-8" } })

const notFound = (): Response => jsonResponse({ error: "not found" }, 404)

const parseOffset = (value: string | null): number => {
  if (value === null) return 0
  const parsed = Number(value)
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : 0
}

const parseSeqId = (stem: string): number | null => /^\d{12}$/.test(stem) ? Number(stem) : null

const compareByName = (left: { readonly name: string }, right: { readonly name: string }): number =>
  left.name.localeCompare(right.name, undefined, { numeric: true })

export async function GET(request: Request): Promise<Response> {
  const auth = authenticateInternalRequest(request)
  if (auth.kind !== "authorized") {
    const failure = internalAuthFailure(auth)
    return new Response(failure.body, failure)
  }

  let blobRoot: string
  try {
    blobRoot = loadLibertreeReadOnlyDataPaths(process.env).blobRoot
  } catch {
    return jsonResponse({ error: "internal catalogue is not configured" }, 503)
  }

  const url = new URL(request.url)
  const rawPath = url.searchParams.get("path") ?? ""
  if (rawPath.includes("\u0000")) return notFound()
  const offset = parseOffset(url.searchParams.get("offset"))

  const target = join(blobRoot, rawPath)
  let resolved: string
  try {
    resolved = realpathSync.native(target)
  } catch {
    return notFound()
  }
  if (resolved !== blobRoot && !resolved.startsWith(blobRoot + sep)) return notFound()

  let stats: ReturnType<typeof statSync>
  try {
    stats = statSync(resolved)
  } catch {
    return notFound()
  }

  if (stats.isFile()) return jsonResponse({ error: "not a directory" }, 400)
  if (!stats.isDirectory()) return notFound()

  const normalizedRelPath = resolved === blobRoot ? "" : resolved.slice(blobRoot.length + 1)

  const dirents = readdirSync(resolved, { withFileTypes: true })
  const dirs: DirEntry[] = []
  const files: FileEntry[] = []
  for (const dirent of dirents) {
    if (dirent.isDirectory()) {
      dirs.push({ type: "dir", name: dirent.name, relPath: normalizedRelPath === "" ? dirent.name : `${normalizedRelPath}/${dirent.name}` })
    } else if (dirent.isFile()) {
      const dotIndex = dirent.name.lastIndexOf(".")
      const stem = dotIndex === -1 ? dirent.name : dirent.name.slice(0, dotIndex)
      const ext = (dotIndex === -1 ? "" : dirent.name.slice(dotIndex + 1)).toLowerCase()
      let size: number
      try {
        size = statSync(join(resolved, dirent.name)).size
      } catch {
        continue
      }
      const seqId = parseSeqId(stem)
      const blobHref = seqId !== null && (ext === "pdf" || ext === "txt") ? `/api/blob/${seqId}/${ext}` : null
      files.push({ type: "file", name: dirent.name, size, ext, seqId, blobHref })
    }
  }
  dirs.sort(compareByName)
  files.sort(compareByName)
  const combined: readonly Entry[] = [...dirs, ...files]
  const total = combined.length
  const page = combined.slice(offset, offset + LIMIT)

  return jsonResponse({ path: normalizedRelPath, isRoot: normalizedRelPath === "", entries: page, total, offset, limit: LIMIT }, 200)
}
