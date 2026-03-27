import { Suspense } from 'react';
import { getPapers } from '@/lib/db';
import PaperList from '@/components/PaperList';
import SearchBar from '@/components/SearchBar';
import SiteFilter from '@/components/SiteFilter';

export default function Home({
  searchParams,
}: {
  searchParams: { site?: string; q?: string };
}) {
  const siteId = searchParams.site;
  const search = searchParams.q;

  let papers: any[] = [];
  let total = 0;
  let sites: any[] = [];
  let dbError = false;

  try {
    const result = getPapers({ siteId, search, page: 1, pageSize: 20 });
    papers = result.papers;
    total = result.total;
    sites = result.sites;
  } catch (e) {
    dbError = true;
  }

  return (
    <div className="space-y-6">
      <Suspense fallback={null}>
        <SearchBar initialSearch={search} />
      </Suspense>
      <Suspense fallback={null}>
        <SiteFilter sites={sites} currentSiteId={siteId} />
      </Suspense>

      {dbError ? (
        <div className="text-center py-12 text-gray-500">
          <p className="text-lg font-medium">데이터베이스를 찾을 수 없습니다</p>
          <p className="mt-2">먼저 크롤러를 실행해주세요:</p>
          <code className="mt-2 block bg-gray-100 p-3 rounded text-sm">
            python crawler/main.py crawl ntrs --limit 10
          </code>
        </div>
      ) : (
        <PaperList
          initialPapers={papers}
          initialTotal={total}
          siteId={siteId}
          search={search}
        />
      )}
    </div>
  );
}
