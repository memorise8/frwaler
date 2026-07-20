import { Fragment } from "react";
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
  params: Promise<{ id: string }>;
}

function padSeq(seqId: number): string {
  return seqId.toString().padStart(12, "0");
}

function formatDateTime(iso: string | null): string | null {
  if (!iso) return null;
  try {
    return new Date(iso).toLocaleString("ko-KR", {
      dateStyle: "medium",
      timeStyle: "short",
    });
  } catch {
    return iso;
  }
}

function formatBytes(n: number | null): string | null {
  if (n === null || n === undefined) return null;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}

export default async function PaperDetailPage({ params }: Props) {
  const { id } = await params;
  if (!/^\d+$/.test(id)) notFound();
  const seqId = Number.parseInt(id, 10);
  const doc = getDocumentDetail(seqId);
  if (!doc) notFound();

  return (
    <div className="space-y-6">
      <div className="text-sm">
        <Link
          href="/search"
          className="text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-300"
        >
          ← 검색 결과로
        </Link>
      </div>

      <Header doc={doc} />
      <SiteCard doc={doc} />
      <MetadataCard doc={doc} />
      <AbstractCard doc={doc} />
      <SummaryCard doc={doc} />
      <CollectionStatusCard doc={doc} />

      <div className="text-xs text-right">
        <Link
          href={`/admin/document/${padSeq(doc.seq_id)}`}
          className="text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200 underline-offset-2 hover:underline"
        >
          관리자용 검증 페이지 →
        </Link>
      </div>
    </div>
  );
}

function Header({ doc }: { doc: DocumentDetail }) {
  const collected = formatDateTime(doc.collected_at);
  const padded = padSeq(doc.seq_id);
  return (
    <div className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 p-6 space-y-4">
      <h1 className="text-xl font-bold text-slate-900 dark:text-slate-100 leading-snug">
        {doc.title || "(제목 없음)"}
      </h1>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500 dark:text-slate-400">
        <span>
          <span className="text-slate-400 dark:text-slate-500">seq_id:</span>{" "}
          <span className="font-mono text-slate-700 dark:text-slate-300">
            {padded}
          </span>
        </span>
        {collected && (
          <span>
            <span className="text-slate-400 dark:text-slate-500">수집일:</span>{" "}
            {collected}
          </span>
        )}
      </div>

      <div className="flex flex-wrap gap-2">
        {doc.meta_url && (
          <a
            href={doc.meta_url}
            target="_blank"
            rel="noopener noreferrer"
            className="px-3 py-1.5 border border-slate-300 dark:border-slate-700 rounded-lg text-xs font-medium hover:bg-slate-50 dark:hover:bg-slate-800"
          >
            원문 ↗
          </a>
        )}
        {doc.blob_pdf_exists && (
          <a
            href={`/api/blob/${padded}/pdf`}
            className="px-3 py-1.5 bg-blue-600 text-white rounded-lg text-xs font-medium hover:bg-blue-700"
          >
            PDF 다운로드
          </a>
        )}
        {doc.blob_txt_exists && (
          <a
            href={`/api/blob/${padded}/txt`}
            className="px-3 py-1.5 bg-emerald-600 text-white rounded-lg text-xs font-medium hover:bg-emerald-700"
          >
            TXT 다운로드
          </a>
        )}
      </div>
    </div>
  );
}

function SiteCard({ doc }: { doc: DocumentDetail }) {
  return (
    <section className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 p-6 space-y-3">
      <h2 className="text-sm font-semibold text-slate-700 dark:text-slate-300">
        사이트
      </h2>
      <dl className="grid grid-cols-1 sm:grid-cols-[10rem_1fr] gap-x-4 gap-y-2 text-sm">
        <DT>사이트명</DT>
        <DD>
          {doc.site_name ? (
            <>
              <span className="font-medium text-slate-900 dark:text-slate-100">
                {doc.site_name}
              </span>{" "}
              <span className="font-mono text-xs text-slate-500 dark:text-slate-400">
                ({doc.site_id})
              </span>
            </>
          ) : (
            <span className="font-mono text-xs">{doc.site_id}</span>
          )}
        </DD>
        {doc.site_url && (
          <>
            <DT>사이트 URL</DT>
            <DD>
              <a
                href={doc.site_url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-blue-600 dark:text-blue-400 hover:underline break-all"
              >
                {doc.site_url}
              </a>
            </DD>
          </>
        )}
        {doc.post_number && (
          <>
            <DT>글번호</DT>
            <DD>
              <span className="font-mono text-xs">{doc.post_number}</span>
            </DD>
          </>
        )}
        {doc.listed_date && (
          <>
            <DT>list 게시일</DT>
            <DD>{doc.listed_date}</DD>
          </>
        )}
      </dl>
    </section>
  );
}

function MetadataCard({ doc }: { doc: DocumentDetail }) {
  const authors = splitAuthors(doc.authors);
  const publishers = splitPublishers(doc.publisher);
  const keywords = splitKeywords(doc.keywords);

  const rows: Array<{ label: string; node: React.ReactNode }> = [];
  if (doc.published_date) {
    rows.push({ label: "작성일/출판일", node: doc.published_date });
  }
  if (doc.journal) {
    rows.push({ label: "저널", node: doc.journal });
  }
  if (publishers.length > 0) {
    rows.push({
      label: "발행기관",
      node: <BadgeList items={publishers} tone="indigo" />,
    });
  }
  if (authors.length > 0) {
    rows.push({
      label: "저자",
      node: <BadgeList items={authors} tone="slate" />,
    });
  }
  if (keywords.length > 0) {
    rows.push({
      label: "키워드",
      node: <BadgeList items={keywords} tone="emerald" />,
    });
  }
  if (doc.pdf_url) {
    rows.push({
      label: "PDF 원본 URL",
      node: (
        <a
          href={doc.pdf_url}
          target="_blank"
          rel="noopener noreferrer"
          className="text-blue-600 dark:text-blue-400 hover:underline break-all font-mono text-xs"
        >
          {doc.pdf_url}
        </a>
      ),
    });
  }
  if (doc.original_filename) {
    rows.push({
      label: "원본 파일명",
      node: <span className="font-mono text-xs">{doc.original_filename}</span>,
    });
  }

  if (rows.length === 0) return null;

  return (
    <section className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 p-6 space-y-3">
      <h2 className="text-sm font-semibold text-slate-700 dark:text-slate-300">
        메타데이터
      </h2>
      <dl className="grid grid-cols-1 sm:grid-cols-[10rem_1fr] gap-x-4 gap-y-2 text-sm">
        {rows.map((r) => (
          <Fragment key={r.label}>
            <DT>{r.label}</DT>
            <DD>{r.node}</DD>
          </Fragment>
        ))}
      </dl>
    </section>
  );
}

function AbstractCard({ doc }: { doc: DocumentDetail }) {
  if (!doc.abstract || !doc.abstract.trim()) return null;
  return (
    <section className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 p-6 space-y-3">
      <h2 className="text-sm font-semibold text-slate-700 dark:text-slate-300">
        본문(abstract)
      </h2>
      <div className="text-sm text-slate-800 dark:text-slate-200 whitespace-pre-wrap leading-relaxed p-4 bg-slate-50 dark:bg-slate-800/50 rounded-lg border border-slate-100 dark:border-slate-800">
        {doc.abstract}
      </div>
    </section>
  );
}

function SummaryCard({ doc }: { doc: DocumentDetail }) {
  const hasSummary = !!doc.summary?.trim();
  return (
    <section className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 p-6 space-y-3">
      <h2 className="text-sm font-semibold text-slate-700 dark:text-slate-300 flex items-center gap-2">
        AI 요약
        {hasSummary ? (
          <span className="text-xs px-2 py-0.5 rounded bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-300 font-medium">
            요약됨
          </span>
        ) : (
          <span className="text-xs px-2 py-0.5 rounded bg-slate-100 dark:bg-slate-800 text-slate-500 dark:text-slate-400 font-medium">
            (요약 없음)
          </span>
        )}
      </h2>
      {hasSummary ? (
        <div className="text-sm text-slate-800 dark:text-slate-200 whitespace-pre-wrap leading-relaxed p-4 bg-slate-50 dark:bg-slate-800/50 rounded-lg border border-slate-100 dark:border-slate-800">
          {doc.summary}
        </div>
      ) : (
        <p className="text-sm text-slate-500 dark:text-slate-400">
          이 문서에 대한 AI 요약은 아직 생성되지 않았습니다.
        </p>
      )}
      {hasSummary && doc.summary_at && (
        <div className="text-xs text-slate-400 dark:text-slate-500 font-mono">
          at: {doc.summary_at}
        </div>
      )}
    </section>
  );
}

function CollectionStatusCard({ doc }: { doc: DocumentDetail }) {
  const pdfFlag = doc.pdf_downloaded === 1;
  const txtFlag = doc.text_extracted === 1;
  const pdfSize = formatBytes(doc.pdf_size_bytes ?? doc.blob_pdf_disk_size);
  return (
    <section className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 p-6 space-y-3">
      <h2 className="text-sm font-semibold text-slate-700 dark:text-slate-300">
        수집 상태
      </h2>
      <ul className="space-y-2 text-sm">
        <li className="flex items-center gap-2">
          <Badge ok={pdfFlag && doc.blob_pdf_exists}>
            PDF {pdfFlag ? "다운로드됨" : "미수집"}
            {pdfSize ? ` · ${pdfSize}` : ""}
            {pdfFlag && !doc.blob_pdf_exists ? " · ⚠ 파일 누락" : ""}
          </Badge>
          <code className="font-mono text-xs text-slate-500 dark:text-slate-400 break-all">
            {doc.blob_pdf_path}
          </code>
        </li>
        <li className="flex items-center gap-2">
          <Badge ok={txtFlag && doc.blob_txt_exists}>
            TXT {txtFlag ? "추출됨" : "미변환"}
            {txtFlag && !doc.blob_txt_exists ? " · ⚠ 파일 누락" : ""}
          </Badge>
          <code className="font-mono text-xs text-slate-500 dark:text-slate-400 break-all">
            {doc.blob_txt_path}
          </code>
        </li>
      </ul>
    </section>
  );
}

function DT({ children }: { children: React.ReactNode }) {
  return (
    <dt className="text-xs font-medium text-slate-500 dark:text-slate-400 pt-0.5">
      {children}
    </dt>
  );
}
function DD({ children }: { children: React.ReactNode }) {
  return <dd className="text-slate-800 dark:text-slate-200">{children}</dd>;
}

function Badge({
  ok,
  children,
}: {
  ok: boolean;
  children: React.ReactNode;
}) {
  const cls = ok
    ? "bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-300"
    : "bg-slate-100 dark:bg-slate-800 text-slate-500 dark:text-slate-400";
  return (
    <span
      className={`inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded font-medium ${cls}`}
    >
      {ok ? "✓" : "·"} {children}
    </span>
  );
}

function BadgeList({
  items,
  tone,
}: {
  items: string[];
  tone: "slate" | "indigo" | "emerald";
}) {
  const cls = {
    slate:
      "bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-300",
    indigo:
      "bg-indigo-100 dark:bg-indigo-900/40 text-indigo-700 dark:text-indigo-300",
    emerald:
      "bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-300",
  }[tone];
  return (
    <ul className="flex flex-wrap gap-1.5">
      {items.map((item, i) => (
        <li
          key={i}
          className={`px-2 py-0.5 rounded text-xs ${cls}`}
        >
          {item}
        </li>
      ))}
    </ul>
  );
}
