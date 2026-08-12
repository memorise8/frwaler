import "server-only";

export type DocumentItem = Readonly<{
  seq_id: number;
  site_id: string;
  site_name: string;
  country: string;
  doc_type: string;
  title: string;
  published_date: string | null;
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

type Facet = Readonly<{ value: string; count: number; label?: string }>;
export type DocumentCatalogue = Readonly<{
  items: readonly DocumentItem[];
  pagination: Readonly<{ page: number; page_size: number; total: number; pages: number }>;
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
  | Readonly<{ ok: false; kind: "invalid" | "unavailable" }>;

const backendUrl = (): string => (process.env.BE_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");

export async function getDocumentCatalogue(params: URLSearchParams): Promise<CatalogueResult> {
  try {
    const response = await fetch(`${backendUrl()}/documents?${params}`, {
      cache: "no-store",
      signal: AbortSignal.timeout(8000),
    });
    if (response.status === 422) return { ok: false, kind: "invalid" };
    if (!response.ok) return { ok: false, kind: "unavailable" };
    return { ok: true, data: await response.json() as DocumentCatalogue };
  } catch {
    return { ok: false, kind: "unavailable" };
  }
}
