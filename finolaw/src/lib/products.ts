import crypto from "node:crypto";
import path from "node:path";
import Database from "better-sqlite3";

const PROJECT_ROOT = path.resolve(process.cwd(), "..");
const DB_PATH = process.env.FINOLAW_DB_PATH
  ? path.resolve(PROJECT_ROOT, process.env.FINOLAW_DB_PATH)
  : path.join(PROJECT_ROOT, "data", "data.db");

export interface ProductRow {
  id: string;
  source: string;
  external_id: string;
  source_query: string | null;
  manufacturer_part_number: string | null;
  mouser_part_number: string | null;
  manufacturer: string | null;
  description: string | null;
  category: string | null;
  availability: string | null;
  price: string | null;
  currency: string | null;
  datasheet_url: string | null;
  product_url: string | null;
  image_url: string | null;
  lifecycle_status: string | null;
  rohs_status: string | null;
  raw_json: string;
  imported_at: string;
  updated_at: string;
}

export interface ProductImportResult {
  fileName: string;
  rowsRead: number;
  inserted: number;
  updated: number;
  skipped: number;
  errors: string[];
}

type CsvRow = Record<string, string>;

const FIELD_ALIASES: Record<string, string[]> = {
  manufacturer_part_number: [
    "manufacturerpartnumber",
    "manufacturerpartno",
    "mfrpartnumber",
    "mfrpartno",
    "mfrpart",
    "제조사부품번호",
    "제조업체부품번호",
  ],
  mouser_part_number: [
    "mouserpartnumber",
    "mouserpartno",
    "mousernumber",
    "mouserpn",
    "마우저부품번호",
    "mouser부품번호",
  ],
  manufacturer: ["manufacturer", "mfr", "제조사", "제조업체", "maker"],
  description: ["description", "productdescription", "desc", "설명", "제품설명"],
  category: ["category", "productcategory", "카테고리", "분류"],
  availability: ["availability", "stock", "instock", "재고", "가용성"],
  price: ["price", "pricing", "unitprice", "가격", "단가"],
  currency: ["currency", "통화"],
  datasheet_url: ["datasheeturl", "datasheet", "datasheetlink", "data sheet", "데이터시트"],
  product_url: [
    "productdetailurl",
    "producturl",
    "productlink",
    "url",
    "link",
    "제품url",
    "제품링크",
  ],
  image_url: ["imageurl", "image", "imagepath", "이미지"],
  lifecycle_status: ["lifecyclestatus", "lifecycle", "수명주기", "상태"],
  rohs_status: ["rohsstatus", "rohs", "rohs상태"],
};

function writableDb() {
  return new Database(DB_PATH);
}

export function ensureProductsSchema(db: Database.Database): void {
  db.exec(`
    CREATE TABLE IF NOT EXISTS products (
      id TEXT PRIMARY KEY,
      source TEXT NOT NULL,
      external_id TEXT NOT NULL,
      source_query TEXT,
      manufacturer_part_number TEXT,
      mouser_part_number TEXT,
      manufacturer TEXT,
      description TEXT,
      category TEXT,
      availability TEXT,
      price TEXT,
      currency TEXT,
      datasheet_url TEXT,
      product_url TEXT,
      image_url TEXT,
      lifecycle_status TEXT,
      rohs_status TEXT,
      raw_json TEXT NOT NULL,
      imported_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      UNIQUE(source, external_id)
    );
    CREATE INDEX IF NOT EXISTS idx_products_source ON products(source);
    CREATE INDEX IF NOT EXISTS idx_products_manufacturer ON products(manufacturer);
    CREATE INDEX IF NOT EXISTS idx_products_updated ON products(updated_at);
  `);
}

function normalizeHeader(value: string): string {
  return value
    .replace(/^\uFEFF/, "")
    .toLowerCase()
    .replace(/&/g, "and")
    .replace(/[^a-z0-9가-힣]+/g, "");
}

function cleanCell(value: string | undefined): string {
  return (value ?? "").replace(/\u0000/g, "").trim();
}

function pick(row: CsvRow, field: keyof typeof FIELD_ALIASES): string {
  const aliases = FIELD_ALIASES[field];
  for (const alias of aliases) {
    const key = normalizeHeader(alias);
    const value = cleanCell(row[key]);
    if (value) return value;
  }
  return "";
}

function stableHash(value: unknown): string {
  return crypto
    .createHash("sha1")
    .update(JSON.stringify(value))
    .digest("hex")
    .slice(0, 24);
}

function parseCsvLine(line: string, delimiter: string): string[] {
  const out: string[] = [];
  let value = "";
  let quoted = false;

  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i];
    const next = line[i + 1];
    if (ch === '"') {
      if (quoted && next === '"') {
        value += '"';
        i += 1;
      } else {
        quoted = !quoted;
      }
      continue;
    }
    if (ch === delimiter && !quoted) {
      out.push(value);
      value = "";
      continue;
    }
    value += ch;
  }
  out.push(value);
  return out;
}

function splitRecords(text: string): string[] {
  const records: string[] = [];
  let current = "";
  let quoted = false;

  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    const next = text[i + 1];
    if (ch === '"') {
      if (quoted && next === '"') {
        current += ch + next;
        i += 1;
      } else {
        quoted = !quoted;
        current += ch;
      }
      continue;
    }
    if ((ch === "\n" || ch === "\r") && !quoted) {
      if (current.trim()) records.push(current);
      current = "";
      if (ch === "\r" && next === "\n") i += 1;
      continue;
    }
    current += ch;
  }
  if (current.trim()) records.push(current);
  return records;
}

function detectDelimiter(headerLine: string): string {
  const candidates = [",", "\t", ";"];
  return candidates
    .map((delimiter) => ({
      delimiter,
      count: parseCsvLine(headerLine, delimiter).length,
    }))
    .sort((a, b) => b.count - a.count)[0].delimiter;
}

export function parseCsv(text: string): CsvRow[] {
  const records = splitRecords(text.replace(/^\uFEFF/, ""));
  if (records.length < 2) return [];

  const delimiter = detectDelimiter(records[0]);
  const headers = parseCsvLine(records[0], delimiter).map(normalizeHeader);
  return records.slice(1).map((record) => {
    const cells = parseCsvLine(record, delimiter);
    const row: CsvRow = {};
    headers.forEach((header, index) => {
      if (header) row[header] = cleanCell(cells[index]);
    });
    return row;
  });
}

export function importProductCsv(params: {
  fileName: string;
  text: string;
  source?: string;
  sourceQuery?: string;
}): ProductImportResult {
  const source = cleanCell(params.source) || "mouser";
  const sourceQuery = cleanCell(params.sourceQuery) || null;
  const rows = parseCsv(params.text);
  const result: ProductImportResult = {
    fileName: params.fileName,
    rowsRead: rows.length,
    inserted: 0,
    updated: 0,
    skipped: 0,
    errors: [],
  };

  const db = writableDb();
  try {
    ensureProductsSchema(db);
    const exists = db.prepare(
      `SELECT 1 FROM products WHERE source = ? AND external_id = ?`
    );
    const upsert = db.prepare(`
      INSERT INTO products (
        id, source, external_id, source_query,
        manufacturer_part_number, mouser_part_number, manufacturer,
        description, category, availability, price, currency,
        datasheet_url, product_url, image_url, lifecycle_status, rohs_status,
        raw_json, imported_at, updated_at
      )
      VALUES (
        @id, @source, @external_id, @source_query,
        @manufacturer_part_number, @mouser_part_number, @manufacturer,
        @description, @category, @availability, @price, @currency,
        @datasheet_url, @product_url, @image_url, @lifecycle_status, @rohs_status,
        @raw_json, @imported_at, @updated_at
      )
      ON CONFLICT(source, external_id) DO UPDATE SET
        source_query = excluded.source_query,
        manufacturer_part_number = excluded.manufacturer_part_number,
        mouser_part_number = excluded.mouser_part_number,
        manufacturer = excluded.manufacturer,
        description = excluded.description,
        category = excluded.category,
        availability = excluded.availability,
        price = excluded.price,
        currency = excluded.currency,
        datasheet_url = excluded.datasheet_url,
        product_url = excluded.product_url,
        image_url = excluded.image_url,
        lifecycle_status = excluded.lifecycle_status,
        rohs_status = excluded.rohs_status,
        raw_json = excluded.raw_json,
        updated_at = excluded.updated_at
    `);

    const run = db.transaction(() => {
      for (const row of rows) {
        const manufacturerPartNumber = pick(row, "manufacturer_part_number");
        const mouserPartNumber = pick(row, "mouser_part_number");
        const externalId =
          mouserPartNumber ||
          manufacturerPartNumber ||
          stableHash({ source, sourceQuery, row });
        if (!externalId) {
          result.skipped += 1;
          continue;
        }

        const now = new Date().toISOString();
        const data = {
          id: `${source}:${externalId}`,
          source,
          external_id: externalId,
          source_query: sourceQuery,
          manufacturer_part_number: manufacturerPartNumber || null,
          mouser_part_number: mouserPartNumber || null,
          manufacturer: pick(row, "manufacturer") || null,
          description: pick(row, "description") || null,
          category: pick(row, "category") || null,
          availability: pick(row, "availability") || null,
          price: pick(row, "price") || null,
          currency: pick(row, "currency") || null,
          datasheet_url: pick(row, "datasheet_url") || null,
          product_url: pick(row, "product_url") || null,
          image_url: pick(row, "image_url") || null,
          lifecycle_status: pick(row, "lifecycle_status") || null,
          rohs_status: pick(row, "rohs_status") || null,
          raw_json: JSON.stringify(row),
          imported_at: now,
          updated_at: now,
        };
        const alreadyExists = Boolean(exists.get(source, externalId));
        upsert.run(data);
        if (alreadyExists) result.updated += 1;
        else result.inserted += 1;
      }
    });

    run();
    return result;
  } catch (error) {
    result.errors.push(error instanceof Error ? error.message : String(error));
    return result;
  } finally {
    db.close();
  }
}

export function getProductsSummary() {
  const db = writableDb();
  try {
    ensureProductsSchema(db);
    const total = (db.prepare(`SELECT COUNT(*) as c FROM products`).get() as {
      c: number;
    }).c;
    const manufacturers = (
      db
        .prepare(
          `SELECT COUNT(DISTINCT manufacturer) as c
           FROM products
           WHERE manufacturer IS NOT NULL AND manufacturer != ''`
        )
        .get() as { c: number }
    ).c;
    const lastUpdated = (
      db.prepare(`SELECT MAX(updated_at) as value FROM products`).get() as {
        value: string | null;
      }
    ).value;
    const recent = db
      .prepare(
        `SELECT * FROM products
         ORDER BY updated_at DESC
         LIMIT 20`
      )
      .all() as ProductRow[];
    return { total, manufacturers, lastUpdated, recent };
  } finally {
    db.close();
  }
}
