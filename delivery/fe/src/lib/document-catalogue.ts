import "server-only";

export type DocumentItem = Readonly<{
  seq_id: number;
  site_id: string;
  site_name: string;
  country: string;
  doc_type: string;
  title: string;
  published_date: string | null;
  published_date_raw?: string | null;
  published_date_status?: string | null;
  published_date_precision?: string | null;
  collected_at: string | null;
  authors: string | null;
  publisher: string | null;
  journal: string | null;
  lang: string;
  has_pdf: boolean;
  has_text: boolean;
  has_translation: boolean;
  meta_url: string;
}>;

// count is null when the BE's counts table has not been refreshed yet and it
// can only list the options.
type Facet = Readonly<{ value: string; count: number | null; label?: string }>;
export type DocumentCatalogue = Readonly<{
  items: readonly DocumentItem[];
  pagination: Readonly<{
    // null on cursor pages: there is no page number, only next/prev.
    page: number | null;
    page_size: number;
    total: number;
    total_exact?: boolean;
    // null when total is not exact.
    pages: number | null;
    has_next?: boolean;
    has_prev?: boolean;
    next_cursor?: string | null;
    prev_cursor?: string | null;
  }>;
  total?: Readonly<{ value: number; exact: boolean; source: string }>;
  facets_meta?: Readonly<{ source: string; exact: boolean }>;
  facets: Readonly<{
    countries: readonly Facet[];
    doc_types: readonly Facet[];
    sites: readonly Facet[];
    languages: readonly Facet[];
  }>;
  query: Readonly<{ q: string | null; sort: string }>;
  measured_at: string;
}>;

export type CatalogueResult =
  | Readonly<{ ok: true; data: DocumentCatalogue }>
  | Readonly<{ ok: false; kind: "invalid"; reason?: string }>
  | Readonly<{ ok: false; kind: "unavailable" }>;

// FastAPI 422s carry either a string detail or {reason, message}.
const invalidReason = async (response: Response): Promise<string | undefined> => {
  try {
    const body = await response.json() as { detail?: unknown };
    const detail = body.detail;
    if (detail && typeof detail === "object" && "reason" in detail && typeof detail.reason === "string") return detail.reason;
  } catch {
    // Not JSON: the generic invalid message applies.
  }
  return undefined;
};

const backendUrl = (): string => (process.env.BE_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");

export async function getDocumentCatalogue(params: URLSearchParams): Promise<CatalogueResult> {
  try {
    const response = await fetch(`${backendUrl()}/documents?${params}`, {
      cache: "no-store",
      signal: AbortSignal.timeout(8000),
    });
    if (response.status === 422) return { ok: false, kind: "invalid", reason: await invalidReason(response) };
    if (!response.ok) return { ok: false, kind: "unavailable" };
    return { ok: true, data: await response.json() as DocumentCatalogue };
  } catch {
    return { ok: false, kind: "unavailable" };
  }
}
