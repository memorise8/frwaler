import Link from 'next/link';
import { getPaperById } from '@/lib/db';
import { notFound } from 'next/navigation';

const SITE_NAMES: Record<string, string> = {
  ntrs: 'NASA NTRS',
  mohw: '보건복지부',
};

const SITE_COLORS: Record<string, string> = {
  ntrs: 'bg-indigo-100 text-indigo-800',
  mohw: 'bg-green-100 text-green-800',
};

export default function PaperDetailPage({
  params,
}: {
  params: { id: string };
}) {
  const paper = getPaperById(params.id);

  if (!paper) {
    notFound();
  }

  const authors = paper.authors ? JSON.parse(paper.authors) : [];
  const keywords = paper.keywords ? JSON.parse(paper.keywords) : [];
  const badgeColor = SITE_COLORS[paper.site_id] || 'bg-gray-100 text-gray-800';
  const siteName = SITE_NAMES[paper.site_id] || paper.site_id;

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      {/* Back button */}
      <Link href="/papers" className="text-sm text-blue-600 hover:text-blue-800">
        ← 목록으로 돌아가기
      </Link>

      {/* Header */}
      <div className="bg-white rounded-xl border border-gray-200 p-6 space-y-4">
        <div className="flex items-center gap-2 flex-wrap">
          <span className={`px-3 py-1 rounded-full text-sm font-medium ${badgeColor}`}>
            {siteName}
          </span>
          {paper.category && (
            <span className="px-3 py-1 rounded-full text-sm font-medium bg-gray-100 text-gray-600">
              {paper.category}
            </span>
          )}
          {paper.published_date && (
            <span className="text-sm text-gray-500">{paper.published_date}</span>
          )}
        </div>

        <h1 className="text-2xl font-bold text-gray-900">{paper.title}</h1>

        {authors.length > 0 && (
          <p className="text-gray-600">{authors.join(', ')}</p>
        )}
        {paper.department && (
          <p className="text-gray-600">담당부서: {paper.department}</p>
        )}

        {/* Action buttons */}
        <div className="flex gap-3 pt-2">
          <a
            href={paper.url}
            target="_blank"
            rel="noopener noreferrer"
            className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors text-sm font-medium"
          >
            원본 페이지 열기 ↗
          </a>
          {paper.pdf_url && (
            <a
              href={paper.pdf_url}
              target="_blank"
              rel="noopener noreferrer"
              className="px-4 py-2 bg-red-50 text-red-700 rounded-lg hover:bg-red-100 transition-colors text-sm font-medium"
            >
              PDF 다운로드
            </a>
          )}
          {paper.doi && (
            <a
              href={`https://doi.org/${paper.doi}`}
              target="_blank"
              rel="noopener noreferrer"
              className="px-4 py-2 bg-yellow-50 text-yellow-700 rounded-lg hover:bg-yellow-100 transition-colors text-sm font-medium"
            >
              DOI 링크
            </a>
          )}
        </div>
      </div>

      {/* AI Summary */}
      {paper.summary ? (
        <div className="bg-white rounded-xl border border-gray-200 p-6">
          <div className="flex items-center gap-2 mb-3">
            <span className="text-lg">🤖</span>
            <h2 className="text-lg font-bold text-gray-900">AI 요약</h2>
            <span className="px-2 py-0.5 rounded text-xs bg-purple-100 text-purple-700 font-medium">GPT-4o-mini</span>
          </div>
          <p className="text-gray-700 leading-relaxed whitespace-pre-wrap">{paper.summary}</p>
        </div>
      ) : (
        <div className="bg-gray-50 rounded-xl border border-dashed border-gray-300 p-6 text-center text-gray-400">
          <span className="text-2xl block mb-2">🤖</span>
          <p>아직 AI 요약이 생성되지 않았습니다</p>
          <p className="text-sm mt-1">
            <code>python -m crawler.main summarize --limit 10</code>
          </p>
        </div>
      )}

      {/* Keywords */}
      {keywords.length > 0 && (
        <div className="bg-white rounded-xl border border-gray-200 p-6">
          <h2 className="text-lg font-bold text-gray-900 mb-3">키워드</h2>
          <div className="flex gap-2 flex-wrap">
            {keywords.map((kw: string, i: number) => (
              <span key={i} className="px-3 py-1 bg-blue-50 text-blue-700 text-sm rounded-full">
                {kw}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
