declare module "node:sqlite" {
  export type SqliteRow = {
    readonly [column: string]: unknown
  }

  export interface StatementSync {
    all(...parameters: readonly unknown[]): readonly SqliteRow[]
    get(...parameters: readonly unknown[]): SqliteRow | undefined
  }

  export interface DatabaseSyncOptions {
    readonly allowExtension?: boolean
    readonly readOnly?: boolean
  }

  export class DatabaseSync {
    constructor(location: string, options?: DatabaseSyncOptions)
    close(): void
    prepare(sql: string): StatementSync
  }
}
