import { readFile } from "node:fs/promises"
import { join } from "node:path"

export const dynamic = "force-dynamic"

const DELIVERABLES: Readonly<Record<string, true>> = {
  "dev-environment": true,
  "infrastructure": true,
  "beta-test-report": true,
  "functional-spec": true,
  "test-cases": true,
  "wbs": true,
  "api-spec": true,
  "admin-manual": true,
  "operations-guide": true,
  "completion-report": true,
}

const notFound = (): Response => new Response("Not found", { status: 404 })

export async function GET(_request: Request, { params }: Readonly<{ params: Promise<{ readonly slug: string }> }>): Promise<Response> {
  const { slug } = await params
  if (!Object.hasOwn(DELIVERABLES, slug)) return notFound()
  const path = join(process.cwd(), "src/content/deliverables", `${slug}.html`)
  let html: string
  try {
    html = await readFile(path, "utf8")
  } catch {
    return notFound()
  }
  return new Response(html, { status: 200, headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" } })
}
