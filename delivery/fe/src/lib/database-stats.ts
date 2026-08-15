export type DatabaseStats = Readonly<{
  overview: Readonly<{
    sites: number;
    documents: number;
    pdf_downloaded: number;
    text_extracted: number;
    pdf_bytes: number;
    latest_collected_at: string | null;
  }>;
  by_sheet: ReadonlyArray<Readonly<{ key: string; sites: number; documents: number }>>;
  by_site: ReadonlyArray<Readonly<{ key: string; documents: number }>>;
  integrity: Readonly<{
    orphan_documents: number;
    missing_pdf_metadata: number;
    blob_check: Readonly<{ status: string }>;
  }>;
  measured_at: string;
}>;

const backendUrl = (): string => (process.env.BE_URL ?? "http://127.0.0.1:8080").replace(/\/$/, "");

export async function getDatabaseStats(): Promise<DatabaseStats | null> {
  try {
    const response = await fetch(`${backendUrl()}/stats`, {
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    });
    if (!response.ok) return null;
    return await response.json() as DatabaseStats;
  } catch {
    return null;
  }
}

export type FreshnessSite = Readonly<{
  site_id: string;
  site_name: string;
  sheet: string;
  documents: number;
  last_collected_at: string | null;
  age_days: number;
  freshness_bucket: string;
}>;

export type FreshnessStats = Readonly<{
  summary: Readonly<{ total_sites: number; with_documents: number; never_collected: number; distribution: Readonly<Record<string, number>> }>;
  sites: ReadonlyArray<FreshnessSite>;
  measured_at: string;
}>;

export async function getFreshnessStats(): Promise<FreshnessStats | null> {
  try {
    const response = await fetch(`${backendUrl()}/freshness`, { cache: "no-store", signal: AbortSignal.timeout(5000) });
    if (!response.ok) return null;
    return await response.json() as FreshnessStats;
  } catch { return null; }
}
