import Link from 'next/link';
import { getSiteOverview } from '@/lib/db';

const SITE_DESCRIPTIONS: Record<string, string> = {
  ntrs: 'NASA의 기술 보고서, 학술 논문, 컨퍼런스 자료 등을 제공하는 NASA Technical Reports Server',
  mohw: '보건복지부에서 발간하는 정책 자료, 사업 안내, 연구 보고서 등',
};

const SITE_ICONS: Record<string, string> = {
  ntrs: '🚀',
  mohw: '🏥',
};

const SITE_COLORS: Record<string, { bg: string; border: string; badge: string }> = {
  ntrs: { bg: 'bg-indigo-50', border: 'border-indigo-200', badge: 'bg-indigo-100 text-indigo-800' },
  mohw: { bg: 'bg-green-50', border: 'border-green-200', badge: 'bg-green-100 text-green-800' },
};

export default function SitesPage() {
  let sites: any[] = [];
  let dbError = false;

  try {
    sites = getSiteOverview();
  } catch {
    dbError = true;
  }

  return (
    <div className="space-y-8">
      <div>
        <h2 className="text-xl font-bold text-gray-900">연결된 사이트</h2>
        <p className="text-sm text-gray-500 mt-1">클릭하면 해당 사이트의 문서 목록으로 이동합니다</p>
      </div>

      {dbError ? (
        <div className="text-center py-12 text-gray-500">
          <p>데이터베이스를 찾을 수 없습니다. 먼저 크롤러를 실행해주세요.</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          {sites.map((site) => {
            const colors = SITE_COLORS[site.id] || { bg: 'bg-gray-50', border: 'border-gray-200', badge: 'bg-gray-100 text-gray-800' };
            const icon = SITE_ICONS[site.id] || '📄';
            const description = SITE_DESCRIPTIONS[site.id] || '';

            return (
              <Link
                key={site.id}
                href={`/papers?site=${site.id}`}
                className={`block p-6 rounded-xl border-2 ${colors.border} ${colors.bg} hover:shadow-lg transition-all duration-200 hover:-translate-y-1`}
              >
                <div className="flex items-start gap-4">
                  <span className="text-4xl">{icon}</span>
                  <div className="flex-1">
                    <h3 className="text-lg font-bold text-gray-900">{site.name}</h3>
                    <p className="text-sm text-gray-500 mt-1">{site.base_url}</p>
                    {description && (
                      <p className="text-sm text-gray-600 mt-2">{description}</p>
                    )}
                    <div className="flex items-center gap-3 mt-4">
                      <span className={`px-3 py-1 rounded-full text-sm font-medium ${colors.badge}`}>
                        {site.paper_count.toLocaleString()}개 문서
                      </span>
                      {site.last_crawled && (
                        <span className="text-xs text-gray-400">
                          마지막 크롤링: {site.last_crawled}
                        </span>
                      )}
                    </div>
                  </div>
                  <span className="text-gray-300 text-xl">→</span>
                </div>
              </Link>
            );
          })}
        </div>
      )}

      <div className="text-center pt-4">
        <Link href="/papers" className="text-blue-600 hover:text-blue-800 text-sm font-medium">
          전체 문서 보기 →
        </Link>
      </div>
    </div>
  );
}
