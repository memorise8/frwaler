import Link from "next/link";
import { notFound } from "next/navigation";
import {
  getDocumentDetail,
  splitAuthors,
  splitKeywords,
  splitPublishers,
  type DocumentDetail,
} from "@/lib/db";

export const dynamic = "force-dynamic";

interface Props {
  params: Promise<{ seq_id: string }>;
}

function padSeq(seqId: number): string {
  return seqId.toString().padStart(12, "0");
}

function formatBytes(n: number | null): string {
  if (n === null || n === undefined) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}

function rawDisplay(v: unknown): string {
  if (v === null || v === undefined) return "NULL";
  if (typeof v === "string") {
    if (v.length === 0) return '"" (empty)';
    return JSON.stringify(v);
  }
  if (typeof v === "boolean") return v ? "true" : "false";
  return String(v);
}

function charCount(v: string | null): string {
  if (v === null || v === undefined) return "—";
  return `${v.length.toLocaleString("en-US")} chars`;
}

export default async function AdminDocumentPage({ params }: Props) {
  const { seq_id: seqIdRaw } = await params;
  if (!/^\d+$/.test(seqIdRaw)) notFound();
  const seqId = Number.parseInt(seqIdRaw, 10);
  if (!Number.isInteger(seqId) || seqId < 0 || seqId >= 1e12) notFound();

  const doc = getDocumentDetail(seqId);
  if (!doc) notFound();

  const padded = padSeq(doc.seq_id);
  const authors = splitAuthors(doc.authors);
  const publishers = splitPublishers(doc.publisher);
  const keywords = splitKeywords(doc.keywords);

  const rows = buildRows(doc, { authors, publishers, keywords });

  return (
    <div className="space-y-6 font-mono">
      <div className="flex items-center justify-between flex-wrap gap-2 text-sm">
        <div className="flex items-center gap-3">
          <Link
            href={`/search/${padded}`}
            className="text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-300"
          >
            ← /search/{padded}
          </Link>
          <Link
            href="/admin/status"
            className="text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200"
          >
            · /admin/status
          </Link>
        </div>
        <div className="text-xs text-slate-500 dark:text-slate-400">
          관리자 raw 검증 페이지
        </div>
      </div>

      <header className="bg-slate-900 dark:bg-slate-950 text-slate-100 rounded-xl border border-slate-700 dark:border-slate-800 p-5">
        <div className="text-xs uppercase tracking-wider text-slate-400">
          documents
        </div>
        <h1 className="text-lg font-bold mt-1 break-all">
          seq_id = {doc.seq_id} ({padded})
        </h1>
        <div className="mt-2 text-xs text-slate-300">
          site_id = {doc.site_id} · post_number = {doc.post_number ?? "NULL"}
        </div>
      </header>

      <section className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 overflow-hidden">
        <table className="w-full text-xs">
          <thead className="bg-slate-50 dark:bg-slate-800/50">
            <tr>
              <th className="text-left px-3 py-2 font-medium w-44 text-slate-600 dark:text-slate-300">
                필드
              </th>
              <th className="text-left px-3 py-2 font-medium text-slate-600 dark:text-slate-300">
                DB 값
              </th>
              <th className="text-left px-3 py-2 font-medium w-64 text-slate-600 dark:text-slate-300">
                비고
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
            {rows.map((r) => (
              <tr
                key={r.label}
                className="align-top hover:bg-slate-50/60 dark:hover:bg-slate-800/30"
              >
                <td className="px-3 py-2 text-slate-500 dark:text-slate-400 whitespace-nowrap">
                  {r.label}
                </td>
                <td className="px-3 py-2 text-slate-800 dark:text-slate-200 whitespace-pre-wrap break-all">
                  {r.value}
                </td>
                <td className="px-3 py-2 text-slate-500 dark:text-slate-400 whitespace-pre-wrap break-all">
                  {r.note ?? "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 p-5 space-y-3">
        <h2 className="text-sm font-bold text-slate-700 dark:text-slate-200">
          블롭 파일
        </h2>
        <BlobRow
          ext="pdf"
          path={doc.blob_pdf_path}
          exists={doc.blob_pdf_exists}
          dbFlag={doc.pdf_downloaded === 1}
          dbSize={doc.pdf_size_bytes}
          diskSize={doc.blob_pdf_disk_size}
          padded={padded}
        />
        <BlobRow
          ext="txt"
          path={doc.blob_txt_path}
          exists={doc.blob_txt_exists}
          dbFlag={doc.text_extracted === 1}
          dbSize={null}
          diskSize={doc.blob_txt_disk_size}
          padded={padded}
        />
      </section>

      <section className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 p-5 space-y-2">
        <h2 className="text-sm font-bold text-slate-700 dark:text-slate-200">
          raw JSON dump
        </h2>
        <pre className="text-xs p-3 bg-slate-50 dark:bg-slate-950 rounded-lg border border-slate-100 dark:border-slate-800 overflow-x-auto text-slate-700 dark:text-slate-300 leading-relaxed">
          {JSON.stringify(serialize(doc), null, 2)}
        </pre>
      </section>
    </div>
  );
}

interface RowSpec {
  label: string;
  value: React.ReactNode;
  note?: React.ReactNode;
}

function buildRows(
  d: DocumentDetail,
  parsed: { authors: string[]; publishers: string[]; keywords: string[] }
): RowSpec[] {
  return [
    {
      label: "seq_id",
      value: String(d.seq_id),
      note: `12-digit: ${padSeq(d.seq_id)}`,
    },
    {
      label: "collected_at",
      value: rawDisplay(d.collected_at),
    },
    {
      label: "site_id",
      value: rawDisplay(d.site_id),
      note: d.site_name ? `site_name: ${d.site_name}` : "(no JOIN match)",
    },
    {
      label: "post_number",
      value: rawDisplay(d.post_number),
      note: "점진수집 키",
    },
    {
      label: "title",
      value: rawDisplay(d.title),
      note: charCount(d.title),
    },
    {
      label: "published_date",
      value: rawDisplay(d.published_date),
      note: "작성일/출판일",
    },
    {
      label: "listed_date",
      value: rawDisplay(d.listed_date),
      note: "사이트 list 게시일",
    },
    {
      label: "authors",
      value: rawDisplay(d.authors),
      note:
        parsed.authors.length > 0
          ? `${parsed.authors.length}명 분리: ${parsed.authors.join(" | ")}`
          : "(빈값)",
    },
    {
      label: "publisher",
      value: rawDisplay(d.publisher),
      note:
        parsed.publishers.length > 0
          ? `${parsed.publishers.length}기관: ${parsed.publishers.join(" | ")}`
          : "(빈값)",
    },
    {
      label: "journal",
      value: rawDisplay(d.journal),
    },
    {
      label: "meta_url",
      value: d.meta_url ? (
        <a
          href={d.meta_url}
          target="_blank"
          rel="noopener noreferrer"
          className="text-blue-600 dark:text-blue-400 hover:underline"
        >
          {d.meta_url}
        </a>
      ) : (
        rawDisplay(d.meta_url)
      ),
    },
    {
      label: "pdf_url",
      value: d.pdf_url ? (
        <a
          href={d.pdf_url}
          target="_blank"
          rel="noopener noreferrer"
          className="text-blue-600 dark:text-blue-400 hover:underline"
        >
          {d.pdf_url}
        </a>
      ) : (
        rawDisplay(d.pdf_url)
      ),
    },
    {
      label: "keywords",
      value: rawDisplay(d.keywords),
      note:
        parsed.keywords.length > 0
          ? `${parsed.keywords.length}개: ${parsed.keywords.join(" | ")}`
          : "(빈값)",
    },
    {
      label: "abstract",
      value:
        d.abstract === null ? "NULL" : (
          <details>
            <summary className="cursor-pointer text-blue-600 dark:text-blue-400">
              [펼치기 — {d.abstract.length.toLocaleString("en-US")} chars]
            </summary>
            <pre className="mt-2 whitespace-pre-wrap break-all">
              {d.abstract}
            </pre>
          </details>
        ),
      note: charCount(d.abstract),
    },
    {
      label: "original_filename",
      value: rawDisplay(d.original_filename),
    },
    {
      label: "pdf_downloaded",
      value: String(d.pdf_downloaded ?? "NULL"),
      note:
        d.pdf_downloaded === 1
          ? `disk: ${d.blob_pdf_exists ? "✓ exists" : "✗ MISSING"}`
          : "—",
    },
    {
      label: "text_extracted",
      value: String(d.text_extracted ?? "NULL"),
      note:
        d.text_extracted === 1
          ? `disk: ${d.blob_txt_exists ? "✓ exists" : "✗ MISSING"}`
          : "—",
    },
    {
      label: "pdf_size_bytes",
      value: rawDisplay(d.pdf_size_bytes),
      note: formatBytes(d.pdf_size_bytes),
    },
    {
      label: "pdf_sha256",
      value: rawDisplay(d.pdf_sha256),
    },
    {
      label: "summary",
      value:
        d.summary === null || d.summary === "" ? (
          d.summary === "" ? '"" (empty)' : "NULL"
        ) : (
          <details>
            <summary className="cursor-pointer text-blue-600 dark:text-blue-400">
              [펼치기 — {d.summary.length.toLocaleString("en-US")} chars]
            </summary>
            <pre className="mt-2 whitespace-pre-wrap break-all">
              {d.summary}
            </pre>
          </details>
        ),
      note: charCount(d.summary),
    },
    {
      label: "summary_model",
      value: rawDisplay(d.summary_model),
    },
    {
      label: "summary_at",
      value: rawDisplay(d.summary_at),
    },
    {
      label: "site_name (JOIN)",
      value: rawDisplay(d.site_name),
      note: "from sites table",
    },
    {
      label: "site_url (JOIN)",
      value: rawDisplay(d.site_url),
      note: "from sites table",
    },
  ];
}

function BlobRow({
  ext,
  path,
  exists,
  dbFlag,
  dbSize,
  diskSize,
  padded,
}: {
  ext: "pdf" | "txt";
  path: string;
  exists: boolean;
  dbFlag: boolean;
  dbSize: number | null;
  diskSize: number | null;
  padded: string;
}) {
  return (
    <div className="text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <span className="uppercase font-bold text-slate-700 dark:text-slate-200 w-10">
          {ext}
        </span>
        <code className="text-slate-600 dark:text-slate-300 break-all">
          {path}
        </code>
        <span
          className={`px-2 py-0.5 rounded font-medium ${
            exists
              ? "bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-300"
              : "bg-red-100 dark:bg-red-900/40 text-red-700 dark:text-red-300"
          }`}
        >
          {exists ? "✓ exists" : "✗ missing"}
        </span>
        {exists && (
          <a
            href={`/api/blob/${padded}/${ext}`}
            className="text-blue-600 dark:text-blue-400 hover:underline"
          >
            [download]
          </a>
        )}
      </div>
      <div className="mt-1 ml-12 text-slate-500 dark:text-slate-400 space-x-3">
        <span>db_flag={String(dbFlag)}</span>
        <span>db_size={dbSize === null ? "—" : `${dbSize} B (${formatBytes(dbSize)})`}</span>
        <span>disk_size={diskSize === null ? "—" : `${diskSize} B (${formatBytes(diskSize)})`}</span>
      </div>
    </div>
  );
}

/**
 * Strip absolute paths from the JSON dump — they're noise for the admin.
 */
function serialize(d: DocumentDetail) {
  // Build a clean record without the *_abs fields, in stable order.
  const out: Record<string, unknown> = {
    seq_id: d.seq_id,
    collected_at: d.collected_at,
    site_id: d.site_id,
    post_number: d.post_number,
    meta_url: d.meta_url,
    title: d.title,
    published_date: d.published_date,
    listed_date: d.listed_date,
    authors: d.authors,
    publisher: d.publisher,
    journal: d.journal,
    pdf_url: d.pdf_url,
    keywords: d.keywords,
    abstract: d.abstract,
    original_filename: d.original_filename,
    pdf_downloaded: d.pdf_downloaded,
    text_extracted: d.text_extracted,
    pdf_size_bytes: d.pdf_size_bytes,
    pdf_sha256: d.pdf_sha256,
    summary: d.summary,
    summary_model: d.summary_model,
    summary_at: d.summary_at,
    site_name: d.site_name,
    site_url: d.site_url,
    blob_pdf_path: d.blob_pdf_path,
    blob_txt_path: d.blob_txt_path,
    blob_pdf_exists: d.blob_pdf_exists,
    blob_txt_exists: d.blob_txt_exists,
    blob_pdf_disk_size: d.blob_pdf_disk_size,
    blob_txt_disk_size: d.blob_txt_disk_size,
  };
  return out;
}
