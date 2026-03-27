import Link from 'next/link';

interface PaperCardProps {
  id: string;
  title: string;
  url: string;
  siteName: string;
  siteId: string;
  publishedDate?: string | null;
  authors?: string | null;
  department?: string | null;
  category?: string | null;
  keywords?: string | null;
  abstract?: string | null;
  pdfUrl?: string | null;
  doi?: string | null;
  hasSummary?: boolean;
}

const SITE_COLORS: Record<string, string> = {
  ntrs: 'bg-indigo-100 text-indigo-800',
  mohw: 'bg-green-100 text-green-800',
};

export default function PaperCard({
  id, title, url, siteName, siteId, publishedDate, authors, department,
  category, keywords, abstract: abstractText, pdfUrl, doi, hasSummary,
}: PaperCardProps) {
  const parsedAuthors = authors ? JSON.parse(authors) : [];
  const parsedKeywords = keywords ? JSON.parse(keywords) : [];
  const badgeColor = SITE_COLORS[siteId] || 'bg-gray-100 text-gray-800';

  return (
    <div className="border border-gray-200 rounded-lg p-5 hover:shadow-md transition-shadow bg-white">
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1">
          <div className="flex items-center gap-2 mb-2">
            <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${badgeColor}`}>
              {siteName}
            </span>
            {category && (
              <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-600">
                {category}
              </span>
            )}
            {publishedDate && (
              <span className="text-xs text-gray-500">{publishedDate}</span>
            )}
          </div>
          <div className="flex items-start gap-2">
            <Link
              href={`/papers/${id}`}
              className="text-lg font-semibold text-gray-900 hover:text-blue-600 transition-colors"
            >
              {title}
            </Link>
            {hasSummary && (
              <span className="shrink-0 mt-1 px-1.5 py-0.5 text-xs bg-purple-100 text-purple-700 rounded font-medium">
                AI요약
              </span>
            )}
          </div>
          {(parsedAuthors.length > 0 || department) && (
            <p className="text-sm text-gray-500 mt-1">
              {parsedAuthors.length > 0 ? parsedAuthors.slice(0, 3).join(', ') + (parsedAuthors.length > 3 ? ` 외 ${parsedAuthors.length - 3}명` : '') : ''}
              {department && <span>{parsedAuthors.length > 0 ? ' | ' : ''}{department}</span>}
            </p>
          )}
          {abstractText && (
            <p className="text-sm text-gray-600 mt-2 line-clamp-2">{abstractText}</p>
          )}
          {parsedKeywords.length > 0 && (
            <div className="flex gap-1 mt-2 flex-wrap">
              {parsedKeywords.slice(0, 5).map((kw: string, i: number) => (
                <span key={i} className="px-2 py-0.5 bg-blue-50 text-blue-700 text-xs rounded">
                  {kw}
                </span>
              ))}
            </div>
          )}
        </div>
        <div className="flex flex-col gap-1 shrink-0">
          <a
            href={url}
            target="_blank"
            rel="noopener noreferrer"
            className="px-3 py-1.5 text-xs font-medium bg-blue-50 text-blue-700 rounded hover:bg-blue-100 transition-colors"
          >
            원본 ↗
          </a>
          {pdfUrl && (
            <a
              href={pdfUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="px-3 py-1.5 text-xs font-medium bg-red-50 text-red-700 rounded hover:bg-red-100 transition-colors"
            >
              PDF
            </a>
          )}
          {doi && (
            <a
              href={`https://doi.org/${doi}`}
              target="_blank"
              rel="noopener noreferrer"
              className="px-3 py-1.5 text-xs font-medium bg-yellow-50 text-yellow-700 rounded hover:bg-yellow-100 transition-colors"
            >
              DOI
            </a>
          )}
        </div>
      </div>
    </div>
  );
}
