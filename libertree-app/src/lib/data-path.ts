import { DatabaseSync } from "node:sqlite"
import { realpathSync, statSync } from "node:fs"
import { basename, dirname, isAbsolute, join } from "node:path"
import { pathToFileURL } from "node:url"

const PATH_CONTRACT_ERROR_CODES = {
  appModeRequired: "app_mode_required",
  appModeReadOnly: "app_mode_not_read_only",
  environmentRequired: "environment_required",
  absolutePathRequired: "absolute_path_required",
  papersDatabaseForbidden: "papers_database_forbidden",
  databaseOutsideContract: "database_outside_contract",
  blobOutsideContract: "blob_outside_contract",
  databaseUnavailable: "database_unavailable",
  blobUnavailable: "blob_unavailable",
  databaseNotFile: "database_not_file",
  blobNotDirectory: "blob_not_directory",
  schemaInvalid: "schema_invalid",
  databasePreflightFailed: "database_preflight_failed",
} as const

export type LibertreePathContractErrorCode =
  (typeof PATH_CONTRACT_ERROR_CODES)[keyof typeof PATH_CONTRACT_ERROR_CODES]

export class LibertreePathContractError extends Error {
  readonly name = "LibertreePathContractError"
  readonly code: LibertreePathContractErrorCode
  readonly detail: string

  constructor(
    code: LibertreePathContractErrorCode,
    detail: string,
  ) {
    super(`${code}: ${detail}`)
    this.code = code
    this.detail = detail
  }
}

export type LibertreeReadOnlyDataPaths = {
  readonly dbPath: string
  readonly dbUri: string
  readonly blobRoot: string
}

export type LibertreeEnvironment = Readonly<Record<string, string | undefined>>

export const withLibertreeReadOnlyDatabase = <T>(
  environment: LibertreeEnvironment,
  operation: (database: DatabaseSync) => T,
): T => {
  const paths = loadLibertreeReadOnlyDataPaths(environment)
  const database = new DatabaseSync(paths.dbUri, { allowExtension: false, readOnly: true })
  try {
    return operation(database)
  } finally {
    database.close()
  }
}

const contractError = (
  code: LibertreePathContractErrorCode,
  detail: string,
): LibertreePathContractError => new LibertreePathContractError(code, detail)

const requireAbsoluteEnvironmentPath = (
  environment: LibertreeEnvironment,
  variableName: "LIBERTREE_DB_PATH" | "LIBERTREE_BLOB_ROOT",
): string => {
  const value = environment[variableName]
  if (value === undefined || value.length === 0) {
    throw contractError(PATH_CONTRACT_ERROR_CODES.environmentRequired, `${variableName} is required`)
  }
  if (!isAbsolute(value)) {
    throw contractError(PATH_CONTRACT_ERROR_CODES.absolutePathRequired, `${variableName} must be absolute`)
  }
  return value
}

const resolveExistingPath = (
  value: string,
  unavailableCode:
    | typeof PATH_CONTRACT_ERROR_CODES.databaseUnavailable
    | typeof PATH_CONTRACT_ERROR_CODES.blobUnavailable,
): string => {
  try {
    return realpathSync.native(value)
  } catch {
    throw contractError(unavailableCode, "configured path does not resolve")
  }
}

const hasRequiredTables = (database: DatabaseSync): boolean => {
  const tableNames = new Set<string>()
  for (const row of database
    .prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('documents', 'sites')")
    .all()) {
    const name = row["name"]
    if (typeof name === "string") tableNames.add(name)
  }
  if (!tableNames.has("documents") || !tableNames.has("sites")) return false

  for (const row of database.prepare("PRAGMA table_info(documents)").all()) {
    if (row["name"] === "seq_id") return true
  }
  return false
}

const preflightReadOnlyDatabase = (dbPath: string): void => {
  const uri = pathToFileURL(dbPath)
  uri.searchParams.set("mode", "ro")
  // This validates only the checkpointed main-file schema. It must not open the
  // live WAL/SHM sidecars: the returned dbUri remains the non-immutable runtime
  // URI so future catalogue reads can still observe live WAL-backed data.
  uri.searchParams.set("immutable", "1")
  let database: DatabaseSync | undefined
  try {
    database = new DatabaseSync(uri.href, { allowExtension: false, readOnly: true })
    if (!hasRequiredTables(database)) {
      throw contractError(
        PATH_CONTRACT_ERROR_CODES.schemaInvalid,
        "database must contain sites, documents, and documents.seq_id",
      )
    }
  } catch (error) {
    if (error instanceof LibertreePathContractError) throw error
    const detail = error instanceof Error ? error.message : "SQLite read-only preflight failed"
    throw contractError(PATH_CONTRACT_ERROR_CODES.databasePreflightFailed, detail)
  } finally {
    if (database !== undefined) database.close()
  }
}

export const loadLibertreeReadOnlyDataPaths = (
  environment: LibertreeEnvironment,
): LibertreeReadOnlyDataPaths => {
  if (environment["LIBERTREE_APP_MODE"] === undefined) {
    throw contractError(PATH_CONTRACT_ERROR_CODES.appModeRequired, "LIBERTREE_APP_MODE must be read-only")
  }
  if (environment["LIBERTREE_APP_MODE"] !== "read-only") {
    throw contractError(PATH_CONTRACT_ERROR_CODES.appModeReadOnly, "LIBERTREE_APP_MODE must be read-only")
  }

  const configuredDbPath = requireAbsoluteEnvironmentPath(environment, "LIBERTREE_DB_PATH")
  const configuredBlobRoot = requireAbsoluteEnvironmentPath(environment, "LIBERTREE_BLOB_ROOT")
  if (basename(configuredDbPath) === "papers.db") {
    throw contractError(PATH_CONTRACT_ERROR_CODES.papersDatabaseForbidden, "papers.db is never app data")
  }

  const expectedRepositoryRoot = dirname(configuredBlobRoot)
  const expectedBlobRoot = join(expectedRepositoryRoot, "libertree")
  if (configuredBlobRoot !== expectedBlobRoot) {
    throw contractError(PATH_CONTRACT_ERROR_CODES.blobOutsideContract, "blob root must be repository-root/libertree")
  }

  const expectedCurrentDb = join(expectedRepositoryRoot, "data", "libertree.db")
  const expectedFutureDb = join(expectedRepositoryRoot, "libertree-app", "data", "libertree.db")
  if (configuredDbPath !== expectedCurrentDb && configuredDbPath !== expectedFutureDb) {
    throw contractError(
      PATH_CONTRACT_ERROR_CODES.databaseOutsideContract,
      "database must be the current or relocated Libertree database",
    )
  }

  const canonicalBlobRoot = resolveExistingPath(configuredBlobRoot, PATH_CONTRACT_ERROR_CODES.blobUnavailable)
  if (canonicalBlobRoot !== expectedBlobRoot) {
    throw contractError(PATH_CONTRACT_ERROR_CODES.blobOutsideContract, "blob root symlink escapes the repository")
  }
  if (!statSync(canonicalBlobRoot).isDirectory()) {
    throw contractError(PATH_CONTRACT_ERROR_CODES.blobNotDirectory, "blob root must be a directory")
  }

  const canonicalDbPath = resolveExistingPath(configuredDbPath, PATH_CONTRACT_ERROR_CODES.databaseUnavailable)
  if (canonicalDbPath !== expectedCurrentDb && canonicalDbPath !== expectedFutureDb) {
    throw contractError(PATH_CONTRACT_ERROR_CODES.databaseOutsideContract, "database symlink escapes the repository")
  }
  if (!statSync(canonicalDbPath).isFile()) {
    throw contractError(PATH_CONTRACT_ERROR_CODES.databaseNotFile, "database must be a regular file")
  }
  preflightReadOnlyDatabase(canonicalDbPath)

  const dbUri = pathToFileURL(canonicalDbPath)
  dbUri.searchParams.set("mode", "ro")
  return { dbPath: canonicalDbPath, dbUri: dbUri.href, blobRoot: canonicalBlobRoot }
}
