import { readFileSync } from "node:fs"

const REQUIRED_ENVIRONMENT = {
  LIBERTREE_APP_MODE: "read-only",
  LIBERTREE_DB_PATH: "/data/repository/libertree-app/data/libertree.db",
  LIBERTREE_BLOB_ROOT: "/data/repository/libertree",
}

const REQUIRED_MOUNTS = [
  "/data/repository/libertree-app/data",
  "/data/repository/libertree",
]

const isForbiddenEnvironmentName = (name) => (
  name === "OPENAI_API_KEY"
  || name === "ANTHROPIC_API_KEY"
  || name.startsWith("QWEN_")
  || name.startsWith("CRAWLER_")
)

const mountIsReadOnly = (mountInfo, target) => mountInfo
  .split("\n")
  .some((line) => {
    const fields = line.split(" ")
    return fields[4] === target && fields[5]?.split(",").includes("ro")
  })

const failures = []
for (const [name, expected] of Object.entries(REQUIRED_ENVIRONMENT)) {
  if (process.env[name] !== expected) failures.push(`${name} must equal its catalogue-only runtime value`)
}
for (const name of Object.keys(process.env)) {
  if (isForbiddenEnvironmentName(name)) failures.push(`${name} is forbidden in the catalogue runtime`)
}

let mountInfo = ""
try {
  mountInfo = readFileSync("/proc/self/mountinfo", "utf8")
} catch (error) {
  if (error instanceof Error) failures.push("cannot inspect Linux mount flags")
  else throw error
}
for (const target of REQUIRED_MOUNTS) {
  if (!mountIsReadOnly(mountInfo, target)) failures.push(`${target} must be a read-only mount`)
}

if (failures.length > 0) {
  console.error(`Libertree runtime boundary rejected: ${failures.join("; ")}`)
  process.exit(78)
}
