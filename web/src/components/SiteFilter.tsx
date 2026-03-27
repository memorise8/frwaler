'use client';

import { useRouter, useSearchParams } from 'next/navigation';

interface Site {
  id: string;
  name: string;
}

export default function SiteFilter({ sites, currentSiteId }: { sites: Site[]; currentSiteId?: string }) {
  const router = useRouter();
  const searchParams = useSearchParams();

  const handleFilter = (siteId?: string) => {
    const params = new URLSearchParams(searchParams.toString());
    if (siteId) {
      params.set('site', siteId);
    } else {
      params.delete('site');
    }
    params.delete('page');
    router.push(`/papers?${params.toString()}`);
  };

  return (
    <div className="flex gap-2 flex-wrap">
      <button
        onClick={() => handleFilter()}
        className={`px-4 py-2 rounded-full text-sm font-medium transition-colors ${
          !currentSiteId ? 'bg-blue-600 text-white' : 'bg-gray-100 text-gray-700 hover:bg-gray-200'
        }`}
      >
        전체
      </button>
      {sites.map((site) => (
        <button
          key={site.id}
          onClick={() => handleFilter(site.id)}
          className={`px-4 py-2 rounded-full text-sm font-medium transition-colors ${
            currentSiteId === site.id ? 'bg-blue-600 text-white' : 'bg-gray-100 text-gray-700 hover:bg-gray-200'
          }`}
        >
          {site.name}
        </button>
      ))}
    </div>
  );
}
