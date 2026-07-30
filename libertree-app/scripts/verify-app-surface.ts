import { existsSync, readdirSync, readFileSync, statSync } from "node:fs"
import { join, relative } from "node:path"
import { fileURLToPath } from "node:url"

type SurfaceCheck = {
  readonly name: string
  readonly outcome: "pass" | "fail"
  readonly detail: string
}

const applicationRoot = fileURLToPath(new URL("..", import.meta.url))
const sourceRoot = join(applicationRoot, "src")

const requiredFiles = [
  "src/app/layout.tsx",
  "src/app/page.tsx",
  "src/app/search/page.tsx",
  "src/app/globals.css",
  "DESIGN.md",
] as const

const excludedRouteDirectories = [
  "src/app/admin",
  "src/app/api/crawler",
  "src/app/api/auto-add",
  "src/app/api/smart-find",
  "src/app/auto-add",
  "src/app/crawler",
  "src/app/smart-find",
] as const

const excludedImportFragments = ["crawler", "auto-add", "smart-find", "collection-report"] as const

const listFiles = (directory: string): readonly string[] => readdirSync(directory, { withFileTypes: true })
  .flatMap((entry) => {
    const path = join(directory, entry.name)
    if (entry.isDirectory()) return listFiles(path)
    return entry.isFile() ? [path] : []
  })

const checkRequiredFiles = (): readonly SurfaceCheck[] => requiredFiles.map((path) => ({
  name: `required:${path}`,
  outcome: existsSync(join(applicationRoot, path)) ? "pass" : "fail",
  detail: existsSync(join(applicationRoot, path)) ? "present" : "missing",
}))

const checkExcludedRoutes = (): readonly SurfaceCheck[] => excludedRouteDirectories.map((path) => ({
  name: `excluded-route:${path}`,
  outcome: existsSync(join(applicationRoot, path)) ? "fail" : "pass",
  detail: existsSync(join(applicationRoot, path)) ? "excluded route directory is present" : "absent",
}))

const checkExcludedImports = (): readonly SurfaceCheck[] => {
  const files = listFiles(sourceRoot)
    .filter((path) => /\.(?:ts|tsx)$/.test(path))
    .filter((path) => path !== join(sourceRoot, "lib", "data-path.ts"))
  const failures = files.flatMap((path) => {
    const content = readFileSync(path, "utf8").toLowerCase()
    const match = excludedImportFragments.find((fragment) => content.includes(fragment))
    return match === undefined ? [] : [{ path, match }]
  })
  return [{
    name: "excluded-imports",
    outcome: failures.length === 0 ? "pass" : "fail",
    detail: failures.length === 0
      ? "no excluded control-plane references outside the preserved path contract"
      : failures.map(({ path, match }) => `${relative(applicationRoot, path)} contains ${match}`).join(", "),
  }]
}

const checkDataContract = (): SurfaceCheck => {
  const pathContract = join(sourceRoot, "lib", "data-path.ts")
  const outcome = existsSync(pathContract) && statSync(pathContract).isFile() ? "pass" : "fail"
  return {
    name: "task-3-data-contract",
    outcome,
    detail: outcome === "pass" ? "src/lib/data-path.ts retained" : "path contract is missing",
  }
}

const checks = [
  ...checkRequiredFiles(),
  ...checkExcludedRoutes(),
  ...checkExcludedImports(),
  checkDataContract(),
] as const

console.log(JSON.stringify({ app: "libertree-app", checks }, null, 2))
if (checks.some((check) => check.outcome === "fail")) process.exitCode = 1
