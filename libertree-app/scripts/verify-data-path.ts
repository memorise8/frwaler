import { DatabaseSync } from "node:sqlite"
import { existsSync, mkdtempSync, mkdirSync, rmSync, statSync, symlinkSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import {
  LibertreePathContractError,
  loadLibertreeReadOnlyDataPaths,
  withLibertreeReadOnlyDatabase,
  type LibertreeEnvironment,
  type LibertreePathContractErrorCode,
} from "../src/lib/data-path.ts"

type Candidate = {
  readonly name: "red" | "production"
  readonly load: (environment: LibertreeEnvironment) => void
}

type ScenarioResult = {
  readonly name: string
  readonly outcome: "pass" | "fail"
  readonly detail: string
}

type FileIdentity = {
  readonly exists: boolean
  readonly device?: bigint
  readonly inode?: bigint
  readonly size?: bigint
  readonly mtimeNs?: bigint
  readonly ctimeNs?: bigint
}

const requiredEnvironment = (dbPath: string, blobRoot: string): LibertreeEnvironment => ({
  LIBERTREE_APP_MODE: "read-only",
  LIBERTREE_DB_PATH: dbPath,
  LIBERTREE_BLOB_ROOT: blobRoot,
})

const fileIdentity = (path: string): FileIdentity => {
  if (!existsSync(path)) return { exists: false }
  const stats = statSync(path, { bigint: true })
  return {
    exists: true,
    device: stats.dev,
    inode: stats.ino,
    size: stats.size,
    mtimeNs: stats.mtimeNs,
    ctimeNs: stats.ctimeNs,
  }
}

const sameIdentity = (before: FileIdentity, after: FileIdentity): boolean => (
  before.exists === after.exists
  && before.device === after.device
  && before.inode === after.inode
  && before.size === after.size
  && before.mtimeNs === after.mtimeNs
  && before.ctimeNs === after.ctimeNs
)

const redCandidate: Candidate = {
  name: "red",
  load: (environment) => {
    if (
      environment["LIBERTREE_DB_PATH"] === undefined ||
      environment["LIBERTREE_BLOB_ROOT"] === undefined
    ) {
      throw new LibertreePathContractError("environment_required", "red candidate only rejects missing variables")
    }
  },
}

const expectAccepted = (
  name: string,
  candidate: Candidate,
  environment: LibertreeEnvironment,
): ScenarioResult => {
  try {
    candidate.load(environment)
    return { name, outcome: "pass", detail: "accepted" }
  } catch (error) {
    return { name, outcome: "fail", detail: error instanceof Error ? error.message : "unknown failure" }
  }
}

const expectRejected = (
  name: string,
  candidate: Candidate,
  environment: LibertreeEnvironment,
  expectedCode?: LibertreePathContractErrorCode,
): ScenarioResult => {
  try {
    candidate.load(environment)
    return { name, outcome: "fail", detail: "unsafe configuration was accepted" }
  } catch (error) {
    if (expectedCode === undefined) return { name, outcome: "pass", detail: "rejected" }
    if (error instanceof LibertreePathContractError && error.code === expectedCode) {
      return { name, outcome: "pass", detail: `rejected with ${error.code}` }
    }
    return { name, outcome: "fail", detail: "rejected with an unexpected error" }
  }
}

const createFixture = (root: string): { readonly dbPath: string; readonly blobRoot: string } => {
  const dataRoot = join(root, "data")
  const blobRoot = join(root, "libertree")
  const dbPath = join(dataRoot, "libertree.db")
  mkdirSync(dataRoot, { recursive: true })
  mkdirSync(blobRoot, { recursive: true })
  const database = new DatabaseSync(dbPath)
  database.prepare("CREATE TABLE sites (site_id TEXT PRIMARY KEY)").all()
  database.prepare("CREATE TABLE documents (seq_id INTEGER PRIMARY KEY)").all()
  database.close()
  return { dbPath, blobRoot }
}

const run = (): void => {
  const fixtureRoot = mkdtempSync(join(tmpdir(), "libertree-path-contract-"))
  const escapeRoot = mkdtempSync(join(tmpdir(), "libertree-path-contract-escape-"))
  const externalRoot = mkdtempSync(join(tmpdir(), "libertree-path-contract-external-"))
  const missingBlobRoot = mkdtempSync(join(tmpdir(), "libertree-path-contract-missing-blob-"))
  const nonexistentRoot = mkdtempSync(join(tmpdir(), "libertree-path-contract-nonexistent-"))
  try {
    const fixture = createFixture(fixtureRoot)
    const escapeFixture = createFixture(escapeRoot)
    const externalFixture = createFixture(externalRoot)
    const escapedTarget = externalFixture.dbPath
    const escapeLink = escapeFixture.dbPath
    rmSync(escapeLink)
    symlinkSync(escapedTarget, escapeLink)
    const missingBlobFixture = createFixture(missingBlobRoot)
    rmSync(missingBlobFixture.blobRoot, { recursive: true })
    const nonexistentFixture = createFixture(nonexistentRoot)
    const invalidSchemaPath = join(fixtureRoot, "libertree-app", "data", "libertree.db")
    mkdirSync(join(fixtureRoot, "libertree-app", "data"), { recursive: true })
    const invalidDatabase = new DatabaseSync(invalidSchemaPath)
    invalidDatabase.prepare("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)").all()
    invalidDatabase.close()
    const candidate: Candidate = process.argv.includes("--candidate=red")
      ? redCandidate
      : { name: "production", load: loadLibertreeReadOnlyDataPaths }
    const validFixturePaths = [fixture.dbPath, `${fixture.dbPath}-wal`, `${fixture.dbPath}-shm`]
    const identitiesBeforeValidLoad = validFixturePaths.map((path) => ({
      path,
      identity: fileIdentity(path),
    }))
    const validFixtureResult = expectAccepted(
      "valid fixture",
      candidate,
      requiredEnvironment(fixture.dbPath, fixture.blobRoot),
    )
    const runtimeReadOnlyOpenResult: ScenarioResult = candidate.name === "production"
      ? expectAccepted(
        "runtime read-only catalogue open",
        {
          name: "production",
          load: (environment) => {
            const paths = loadLibertreeReadOnlyDataPaths(environment)
            if (new URL(paths.dbUri).searchParams.has("immutable")) {
              throw new LibertreePathContractError("database_preflight_failed", "runtime URI must remain live-WAL capable")
            }
            withLibertreeReadOnlyDatabase(environment, (database) => {
              database.prepare("SELECT 1 FROM documents LIMIT 1").get()
            })
          },
        },
        requiredEnvironment(fixture.dbPath, fixture.blobRoot),
      )
      : { name: "runtime read-only catalogue open", outcome: "pass", detail: "not applicable to red candidate" }
    const validFixtureIdentityResult: ScenarioResult = identitiesBeforeValidLoad.every(({ path, identity }) => (
      sameIdentity(identity, fileIdentity(path))
    ))
      ? { name: "valid fixture identity", outcome: "pass", detail: "database sidecars unchanged" }
      : { name: "valid fixture identity", outcome: "fail", detail: "database sidecar identity changed" }
    const results = [
      validFixtureResult,
      runtimeReadOnlyOpenResult,
      validFixtureIdentityResult,
      expectRejected("unset database", candidate, { LIBERTREE_APP_MODE: "read-only", LIBERTREE_BLOB_ROOT: fixture.blobRoot }, "environment_required"),
      expectRejected("relative database", candidate, requiredEnvironment("data/libertree.db", fixture.blobRoot), "absolute_path_required"),
      expectRejected("papers database", candidate, requiredEnvironment(join(fixtureRoot, "data", "papers.db"), fixture.blobRoot), "papers_database_forbidden"),
      expectRejected("nonexistent database", candidate, requiredEnvironment(join(nonexistentRoot, "libertree-app", "data", "libertree.db"), nonexistentFixture.blobRoot), "database_unavailable"),
      expectRejected("missing blob root", candidate, requiredEnvironment(missingBlobFixture.dbPath, missingBlobFixture.blobRoot), "blob_unavailable"),
      expectRejected("symlink escape", candidate, requiredEnvironment(escapeLink, escapeFixture.blobRoot), "database_outside_contract"),
      expectRejected("wrong schema", candidate, requiredEnvironment(invalidSchemaPath, fixture.blobRoot), "schema_invalid"),
      expectRejected("writable app mode", candidate, { ...requiredEnvironment(fixture.dbPath, fixture.blobRoot), LIBERTREE_APP_MODE: "read-write" }, "app_mode_not_read_only"),
    ]
    console.log(JSON.stringify({ candidate: candidate.name, results }))
    if (results.some((result) => result.outcome === "fail")) process.exitCode = 1
  } finally {
    rmSync(fixtureRoot, { recursive: true, force: true })
    rmSync(escapeRoot, { recursive: true, force: true })
    rmSync(externalRoot, { recursive: true, force: true })
    rmSync(missingBlobRoot, { recursive: true, force: true })
    rmSync(nonexistentRoot, { recursive: true, force: true })
  }
}

run()
