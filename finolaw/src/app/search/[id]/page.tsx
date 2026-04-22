import Link from "next/link";
import { notFound } from "next/navigation";
import { getPaper, parseMetadata } from "@/lib/db";

export const dynamic = "force-dynamic";

interface Props {
  params: Promise<{ id: string }>;
}

export default async function PaperDetailPage({ params }: Props) {
  const { id } = await params;
  const paper = getPaper(id);
  if (!paper) notFound();
  const md = parseMetadata(paper.metadata);

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

      <div className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 p-6 space-y-4">
        <div className="flex items-start justify-between gap-3">
          <h1 className="text-xl font-bold text-slate-900 dark:text-slate-100 leading-snug flex-1">
            {paper.title || "(제목 없음)"}
          </h1>
          {paper.url && (
            <a
              href={paper.url}
              target="_blank"
              rel="noopener noreferrer"
              className="shrink-0 px-3 py-1.5 border border-slate-300 dark:border-slate-700 rounded-lg text-xs font-medium hover:bg-slate-50 dark:hover:bg-slate-800 whitespace-nowrap"
            >
              원문 ↗
            </a>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2 text-xs">
          {md.documentTypeName && (
            <span className="px-2.5 py-1 bg-blue-100 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300 rounded font-medium">
              {md.documentTypeName}
            </span>
          )}
          {paper.category && (
            <span className="px-2.5 py-1 bg-emerald-100 dark:bg-emerald-900/40 text-emerald-700 dark:text-emerald-300 rounded font-medium">
              {paper.category}
            </span>
          )}
          {md.documentNumber && (
            <span className="px-2.5 py-1 bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-300 rounded font-mono">
              {md.documentNumber}
            </span>
          )}
          {paper.published_date && (
            <span className="text-slate-500 dark:text-slate-400">
              생산일: {paper.published_date}
            </span>
          )}
          <span className="text-slate-400 dark:text-slate-500">
            · site: {paper.site_id}
          </span>
        </div>

        {paper.abstract && (
          <div>
            <h2 className="text-sm font-semibold text-slate-700 dark:text-slate-300 mb-2">
              본문
            </h2>
            <div className="text-sm text-slate-800 dark:text-slate-200 whitespace-pre-wrap leading-relaxed p-4 bg-slate-50 dark:bg-slate-800/50 rounded-lg border border-slate-100 dark:border-slate-800">
              {paper.abstract}
            </div>
          </div>
        )}

        {md.relatedLaws && md.relatedLaws.length > 0 && (
          <MetaList title="관련 법령" items={md.relatedLaws} />
        )}
        {md.trialHistory && md.trialHistory.length > 0 && (
          <MetaList title="심급 이력" items={md.trialHistory} mono />
        )}
        {md.referencedCases && md.referencedCases.length > 0 && (
          <MetaList title="참조 판례" items={md.referencedCases} mono />
        )}
        {md.citedCases && md.citedCases.length > 0 && (
          <MetaList title="인용 판례" items={md.citedCases} mono />
        )}
        {md.relatedTopics && md.relatedTopics.length > 0 && (
          <MetaList title="관련 주제" items={md.relatedTopics} />
        )}

        <details className="text-xs">
          <summary className="cursor-pointer text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200 font-medium">
            원본 메타데이터 (JSON)
          </summary>
          <pre className="mt-2 p-3 bg-slate-50 dark:bg-slate-800/50 rounded-lg border border-slate-100 dark:border-slate-800 overflow-x-auto font-mono text-slate-700 dark:text-slate-300">
            {JSON.stringify(md, null, 2)}
          </pre>
        </details>
      </div>
    </div>
  );
}

function MetaList({
  title,
  items,
  mono = false,
}: {
  title: string;
  items: string[];
  mono?: boolean;
}) {
  return (
    <div>
      <h2 className="text-sm font-semibold text-slate-700 dark:text-slate-300 mb-2">
        {title}
      </h2>
      <ul className="flex flex-wrap gap-1.5">
        {items.map((item, i) => (
          <li
            key={i}
            className={`px-2.5 py-1 bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-300 rounded text-xs ${
              mono ? "font-mono" : ""
            }`}
          >
            {item}
          </li>
        ))}
      </ul>
    </div>
  );
}
