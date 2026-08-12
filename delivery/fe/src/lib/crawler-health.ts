import "server-only";

import { readFileSync } from "node:fs";
import { join } from "node:path";

export type HealthState = "healthy" | "unhealthy";

export interface CrawlerHealth {
  readonly siteId: string;
  readonly siteName: string;
  readonly status: HealthState;
  readonly category: string;
  readonly reason: string;
  readonly collected: number;
  readonly continent: string;
  readonly country: string;
  readonly docType: string;
}

export const AUDIT_DATE = "2026-08-06";

const parseCsv = (source: string): readonly string[][] => {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  for (let index = 0; index < source.length; index += 1) {
    const char = source[index];
    if (char === '"') {
      if (quoted && source[index + 1] === '"') {
        field += '"';
        index += 1;
      } else quoted = !quoted;
    } else if (char === "," && !quoted) {
      row.push(field);
      field = "";
    } else if ((char === "\n" || char === "\r") && !quoted) {
      if (char === "\r" && source[index + 1] === "\n") index += 1;
      row.push(field);
      if (row.some(Boolean)) rows.push(row);
      row = [];
      field = "";
    } else field += char;
  }
  if (field || row.length) {
    row.push(field);
    rows.push(row);
  }
  return rows;
};

const readFirst = (paths: readonly string[], label: string): string => {
  for (const path of paths) {
    try {
      return readFileSync(path, "utf8");
    } catch {
      // Try the next supported development/deployment location.
    }
  }
  throw new Error(`${label}를 찾을 수 없습니다.`);
};

const repoFile = (...parts: readonly string[]): string => readFirst([
  join(process.cwd(), "..", "..", ...parts),
  join(process.cwd(), ...parts),
], parts.at(-1) ?? "데이터 파일");

const readSource = (): string => readFirst([
    join(process.cwd(), "..", "..", "scripts", "audit", "crawler_status_final.csv"),
    join(process.cwd(), "scripts", "audit", "crawler_status_final.csv"),
    join(process.cwd(), "data", "crawler_status_final.csv"),
  ], "crawler_status_final.csv");

const DOC_TYPE_LABELS: Readonly<Record<string, string>> = {
  etc: "기타", opendata: "공공데이터", paper: "논문", pdfindex: "PDF 검색 색인",
  periodical: "간행물", press: "보도자료", report: "보고서", research: "연구자료", statistics: "통계",
};

const getTaxonomyMaps = () => {
  const [capacityHeader, ...capacityRecords] = parseCsv(repoFile("scripts", "audit", "capacity_final.csv").replace(/^\uFEFF/, ""));
  const sheets = new Map<string, string>();
  if (capacityHeader) {
    const siteColumn = capacityHeader.indexOf("site_id");
    const sheetColumn = capacityHeader.indexOf("sheet");
    for (const record of capacityRecords) sheets.set(record[siteColumn] ?? "", record[sheetColumn] ?? "");
  }
  const countries = new Map<string, { readonly country: string; readonly continent: string }>();
  const categorySource = repoFile("libertree-app", "src", "lib", "categories.ts");
  const categoryPattern = /^\s*(?:"([^"]+)"|([^\s:{]+)):\s*\{\s*country:\s*"([^"]+)",\s*continent:\s*"([^"]+)"/gm;
  for (const match of categorySource.matchAll(categoryPattern)) countries.set(match[1] ?? match[2] ?? "", { country: match[3] ?? "기타", continent: match[4] ?? "Other" });

  const docTypes = new Map<string, string>();
  const docTypeSource = repoFile("libertree-app", "src", "lib", "doc-type-map.generated.ts");
  for (const match of docTypeSource.matchAll(/^\s*"([^"]+)":\s*"([^"]+)"/gm)) docTypes.set(match[1] ?? "", DOC_TYPE_LABELS[match[2] ?? ""] ?? "기타");
  return { countries, docTypes, sheets };
};

export const getCrawlerHealth = (): readonly CrawlerHealth[] => {
  const [header, ...records] = parseCsv(readSource().replace(/^\uFEFF/, ""));
  if (!header) return [];
  const columns = new Map(header.map((name, index) => [name, index]));
  const value = (record: readonly string[], name: string): string => record[columns.get(name) ?? -1] ?? "";
  const taxonomy = getTaxonomyMaps();
  return records.map((record) => {
    const siteId = value(record, "site_id");
    const location = taxonomy.countries.get(taxonomy.sheets.get(siteId) ?? "") ?? { country: "기타", continent: "Other" };
    return {
      siteId,
      siteName: value(record, "site_name").replace(/^Custom:\s*/, ""),
      status: value(record, "status") === "된다" ? "healthy" : "unhealthy",
      category: value(record, "category"), reason: value(record, "reason"),
      collected: Number.parseInt(value(record, "collected"), 10) || 0,
      continent: location.continent, country: location.country,
      docType: taxonomy.docTypes.get(siteId) ?? "기타",
    };
  });
};
