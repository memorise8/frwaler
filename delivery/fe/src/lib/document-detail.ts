import "server-only";

type Translation = Readonly<{
  text: string;
  model_version: string;
  prompt_version: string;
  completed_at: string | null;
}>;

export type DocumentDetail = Readonly<{
  seq_id: number;
  site: Readonly<{ site_id: string; site_name: string; site_url: string; sheet: string | null }>;
  classification: Readonly<{ country: string; doc_type: string }>;
  source: Readonly<{
    lang: string; title: string; abstract: string | null; authors: string | null;
    publisher: string | null; journal: string | null; keywords: string | null;
    published_date: string | null; listed_date: string | null; collected_at: string | null;
    summary: string | null; summary_model: string | null; meta_url: string; pdf_url: string | null;
  }>;
  translations: Readonly<{ target_locale: string; title: Translation | null; description: Translation | null }>;
  generated_summary: Readonly<{ summary_text:string; key_points:string[]; institutions:string[];
    source_facts:Readonly<{urls?:string[];dates?:string[];numbers?:string[]}>;model_version:string;
    prompt_version:string;completed_at:string|null }> | null;
  files: Readonly<{ has_pdf: boolean; has_text: boolean; pdf_size_bytes: number | null; original_filename: string | null }>;
}>;

export type DetailResult =
  | Readonly<{ ok: true; data: DocumentDetail }>
  | Readonly<{ ok: false; kind: "not_found" | "unavailable" }>;

const backendUrl = (): string => (process.env.BE_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");

export async function getDocumentDetail(seqId: string): Promise<DetailResult> {
  try {
    const response = await fetch(`${backendUrl()}/documents/${encodeURIComponent(seqId)}`, {
      cache: "no-store", signal: AbortSignal.timeout(8000),
    });
    if (response.status === 404 || response.status === 422) return { ok: false, kind: "not_found" };
    if (!response.ok) return { ok: false, kind: "unavailable" };
    return { ok: true, data: await response.json() as DocumentDetail };
  } catch {
    return { ok: false, kind: "unavailable" };
  }
}
