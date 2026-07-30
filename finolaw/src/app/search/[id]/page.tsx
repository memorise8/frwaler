import Link from "next/link";
import { notFound } from "next/navigation";
import { getInternalReviewDocument } from "@/lib/db";
import type { InternalReviewDescription } from "@/lib/internal-review";

export const dynamic = "force-dynamic";

interface Props {
  params: Promise<{ id: string }>;
}

export default async function PaperDetailPage({ params }: Props) {
  const { id } = await params;
  if (!/^\d+$/.test(id)) notFound();
  const doc = getInternalReviewDocument(Number.parseInt(id, 10));
  if (!doc) notFound();

  return (
    <div className="min-w-0 max-w-full space-y-6">
      <Link
        href="/search"
        className="inline-flex text-sm text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-300"
      >
        ← 자료 서가로 돌아가기
      </Link>

      <article className="min-w-0 max-w-full bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 p-6 sm:p-8 space-y-6">
        <header className="space-y-4">
          <div className="flex flex-wrap gap-2 text-xs">
            <span className="px-2 py-1 rounded bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-300 font-medium">
              {doc.source.country}
            </span>
            <span className="px-2 py-1 rounded bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300 font-medium">
              {doc.source.category}
            </span>
            <span className="max-w-full break-all px-2 py-1 rounded bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300">
              {doc.source.name}
            </span>
          </div>
          <h1 className="text-2xl sm:text-3xl font-bold text-slate-900 dark:text-slate-100 leading-snug break-keep">
            {doc.title}
          </h1>
          {doc.publishedDate && (
            <p className="text-sm text-slate-500 dark:text-slate-400">
              발행일 {doc.publishedDate}
            </p>
          )}
        </header>

        <DescriptionCard description={doc.description} />

        {(doc.authors.length > 0 || doc.publisher || doc.journal || doc.keywords.length > 0) && (
          <section className="border-t border-slate-100 dark:border-slate-800 pt-6 space-y-4">
            <h2 className="text-sm font-semibold text-slate-700 dark:text-slate-300">서지 정보</h2>
            <dl className="grid grid-cols-1 sm:grid-cols-[7rem_1fr] gap-x-4 gap-y-3 text-sm">
              {doc.authors.length > 0 && <MetadataRow label="저자" value={doc.authors.join(", ")} />}
              {doc.publisher && <MetadataRow label="발행기관" value={doc.publisher} />}
              {doc.journal && <MetadataRow label="저널" value={doc.journal} />}
              {doc.keywords.length > 0 && <MetadataRow label="키워드" value={doc.keywords.join(" · ")} />}
              <MetadataRow label="출처 분류" value={`${doc.source.sheet} · ${doc.source.category}`} />
            </dl>
          </section>
        )}

        <section className="border-t border-slate-100 dark:border-slate-800 pt-6">
          {doc.originalSourceUrl ? (
            <a
              href={doc.originalSourceUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center px-4 py-2.5 rounded-lg bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium"
            >
              원문 출처에서 보기 ↗
            </a>
          ) : (
            <p className="text-sm text-slate-500 dark:text-slate-400">
              검증 가능한 원문 출처 링크가 등록되어 있지 않습니다.
            </p>
          )}
        </section>
      </article>
    </div>
  );
}

function DescriptionCard({ description }: { description: InternalReviewDescription }) {
  const heading = description.provenance === "summary"
    ? "자료 소개 · 요약 기반"
    : description.provenance === "abstract"
      ? "자료 소개 · 초록 기반"
      : "자료 소개";
  return (
    <section className="rounded-xl bg-slate-50 dark:bg-slate-800/50 border border-slate-100 dark:border-slate-800 p-5 space-y-3">
      <h2 className="text-sm font-semibold text-slate-700 dark:text-slate-300">{heading}</h2>
      {description.text ? (
        <p className="text-sm text-slate-800 dark:text-slate-200 whitespace-pre-wrap break-keep leading-relaxed">
          {description.text}
        </p>
      ) : (
        <p className="text-sm text-slate-500 dark:text-slate-400">
          요약 또는 초록 정보가 아직 준비되지 않았습니다.
        </p>
      )}
      {description.truncated && (
        <p className="text-xs text-slate-400 dark:text-slate-500">
          검토 화면에서는 소개문을 일부만 표시합니다.
        </p>
      )}
    </section>
  );
}

function MetadataRow({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt className="text-xs font-medium text-slate-500 dark:text-slate-400 pt-0.5">{label}</dt>
      <dd className="text-slate-800 dark:text-slate-200 break-keep">{value}</dd>
    </>
  );
}
