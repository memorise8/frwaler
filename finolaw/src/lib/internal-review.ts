import { isIP } from "node:net";
import {
  getCategoryForSheet,
  type Continent,
  type SiteFunctionCategory,
} from "@/lib/categories";

const DETAIL_DESCRIPTION_LIMIT = 1_200;
const LIST_DESCRIPTION_LIMIT = 360;
const MAX_KEYWORDS = 12;
const MAX_KEYWORD_LENGTH = 80;
const MAX_SOURCE_LENGTH = 200;
const MAX_TITLE_LENGTH = 500;
const MAX_VALUE_LENGTH = 500;
const MAX_URL_LENGTH = 2_048;

export type DescriptionProvenance = "summary" | "abstract" | "unavailable";

export interface InternalReviewDescription {
  readonly provenance: DescriptionProvenance;
  readonly text: string | null;
  readonly truncated: boolean;
}

export interface InternalReviewSource {
  readonly category: SiteFunctionCategory;
  readonly continent: Continent;
  readonly country: string;
  readonly name: string;
  readonly sheet: string;
}

export interface InternalReviewListItem {
  readonly description: InternalReviewDescription;
  readonly id: number;
  readonly publishedDate: string | null;
  readonly source: InternalReviewSource;
  readonly title: string;
}

export interface InternalReviewDocument extends InternalReviewListItem {
  readonly authors: readonly string[];
  readonly journal: string | null;
  readonly keywords: readonly string[];
  readonly originalSourceUrl: string | null;
  readonly publisher: string | null;
}

export interface InternalReviewRecord {
  readonly abstract: string | null;
  readonly authors: string | null;
  readonly journal: string | null;
  readonly metaUrl: string | null;
  readonly publishedDate: string | null;
  readonly publisher: string | null;
  readonly seqId: number;
  readonly sheet: string | null;
  readonly siteName: string | null;
  readonly siteUrl: string | null;
  readonly summary: string | null;
  readonly title: string | null;
  readonly keywords: string | null;
}

function compactText(value: string | null | undefined): string | null {
  if (!value) return null;
  const compacted = value.replace(/\s+/g, " ").trim();
  return compacted || null;
}

function boundedText(value: string | null | undefined, limit: number): string | null {
  const compacted = compactText(value);
  if (!compacted) return null;
  return compacted.length <= limit ? compacted : `${compacted.slice(0, limit).trimEnd()}…`;
}

function descriptionFrom(record: InternalReviewRecord, limit: number): InternalReviewDescription {
  const summary = compactText(record.summary);
  const abstract = compactText(record.abstract);
  const source = summary ? "summary" : abstract ? "abstract" : "unavailable";
  const selected = summary ?? abstract;
  if (!selected) return { provenance: source, text: null, truncated: false };

  return {
    provenance: source,
    text: boundedText(selected, limit),
    truncated: selected.length > limit,
  };
}

function splitBounded(value: string | null): readonly string[] {
  if (!value) return [];
  return value
    .split(/[;,]/)
    .map((part) => boundedText(part, MAX_KEYWORD_LENGTH))
    .filter((part): part is string => part !== null)
    .slice(0, MAX_KEYWORDS);
}

function normalizedHost(url: URL): string {
  return url.hostname.toLowerCase().replace(/^www\./, "").replace(/\.$/, "");
}

function isPublicDnsName(host: string): boolean {
  return host !== "localhost" && !host.endsWith(".local") && host.includes(".") && isIP(host) === 0;
}

export function vettedOriginalSourceUrl(
  rawUrl: string | null,
  sourceUrl: string | null,
): string | null {
  const candidate = compactText(rawUrl);
  const source = compactText(sourceUrl);
  if (!candidate || !source || candidate.length > MAX_URL_LENGTH || source.length > MAX_URL_LENGTH) {
    return null;
  }

  try {
    const candidateUrl = new URL(candidate);
    const sourceUrlValue = new URL(source);
    if (
      candidateUrl.protocol !== "https:" ||
      sourceUrlValue.protocol !== "https:" ||
      candidateUrl.username ||
      candidateUrl.password ||
      sourceUrlValue.username ||
      sourceUrlValue.password ||
      (candidateUrl.port && candidateUrl.port !== "443") ||
      (sourceUrlValue.port && sourceUrlValue.port !== "443")
    ) {
      return null;
    }

    const candidateHost = normalizedHost(candidateUrl);
    const sourceHost = normalizedHost(sourceUrlValue);
    if (!isPublicDnsName(candidateHost) || candidateHost !== sourceHost) return null;

    return candidateUrl.toString();
  } catch {
    return null;
  }
}

function sourceFrom(record: InternalReviewRecord): InternalReviewSource {
  const category = getCategoryForSheet(record.sheet);
  return {
    category: category.category,
    continent: category.continent,
    country: category.country,
    name: boundedText(record.siteName, MAX_SOURCE_LENGTH) ?? "출처 미분류",
    sheet: boundedText(category.sheet, MAX_SOURCE_LENGTH) ?? "(없음)",
  };
}

export function toInternalReviewListItem(record: InternalReviewRecord): InternalReviewListItem {
  return {
    description: descriptionFrom(record, LIST_DESCRIPTION_LIMIT),
    id: record.seqId,
    publishedDate: boundedText(record.publishedDate, 40),
    source: sourceFrom(record),
    title: boundedText(record.title, MAX_TITLE_LENGTH) ?? "(제목 없음)",
  };
}

export function toInternalReviewDocument(record: InternalReviewRecord): InternalReviewDocument {
  const listItem = toInternalReviewListItem(record);
  return {
    ...listItem,
    authors: splitBounded(record.authors),
    description: descriptionFrom(record, DETAIL_DESCRIPTION_LIMIT),
    journal: boundedText(record.journal, MAX_VALUE_LENGTH),
    keywords: splitBounded(record.keywords),
    originalSourceUrl: vettedOriginalSourceUrl(record.metaUrl, record.siteUrl),
    publisher: boundedText(record.publisher, MAX_VALUE_LENGTH),
  };
}
