import { readFileSync } from "node:fs";
import { join } from "node:path";

export type CrawlerHealthStatus = "healthy" | "unhealthy";

export interface CrawlerHealthRow {
  readonly siteId: string;
  readonly siteName: string;
  readonly status: CrawlerHealthStatus;
  readonly category: string;
  readonly reason: string;
  readonly collected: number;
}

export const CRAWLER_AUDIT_DATE = "2026-08-06";

const csvCandidates = (): readonly string[] => [
  join(process.cwd(), "scripts", "audit", "crawler_status_final.csv"),
  join(process.cwd(), "..", "scripts", "audit", "crawler_status_final.csv"),
];

const parseCsv = (source: string): readonly string[][] => {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;

  for (let index = 0; index < source.length; index += 1) {
    const character = source[index];
    if (character === '"') {
      if (quoted && source[index + 1] === '"') {
        field += '"';
        index += 1;
      } else {
        quoted = !quoted;
      }
    } else if (character === "," && !quoted) {
      row.push(field);
      field = "";
    } else if ((character === "\n" || character === "\r") && !quoted) {
      if (character === "\r" && source[index + 1] === "\n") index += 1;
      row.push(field);
      if (row.some((value) => value.length > 0)) rows.push(row);
      row = [];
      field = "";
    } else {
      field += character;
    }
  }

  if (field.length > 0 || row.length > 0) {
    row.push(field);
    rows.push(row);
  }
  return rows;
};

const readAuditCsv = (): string => {
  const errors: string[] = [];
  for (const path of csvCandidates()) {
    try {
      return readFileSync(path, "utf8");
    } catch (error) {
      errors.push(error instanceof Error ? error.message : String(error));
    }
  }
  throw new Error(`크롤러 상태 CSV를 찾을 수 없습니다. ${errors.join(" | ")}`);
};

export const getCrawlerHealthRows = (): readonly CrawlerHealthRow[] => {
  const [header, ...records] = parseCsv(readAuditCsv().replace(/^\uFEFF/, ""));
  if (!header) return [];
  const column = new Map(header.map((name, index) => [name, index]));
  const value = (record: readonly string[], name: string): string => record[column.get(name) ?? -1] ?? "";

  return records.map((record) => ({
    siteId: value(record, "site_id"),
    siteName: value(record, "site_name").replace(/^Custom:\s*/, ""),
    status: value(record, "status") === "된다" ? "healthy" : "unhealthy",
    category: value(record, "category"),
    reason: value(record, "reason"),
    collected: Number.parseInt(value(record, "collected"), 10) || 0,
  }));
};
