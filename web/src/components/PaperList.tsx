'use client';

import { useState, useEffect, useRef, useCallback } from 'react';
import PaperCard from './PaperCard';

interface Paper {
  id: string;
  site_id: string;
  title: string;
  url: string;
  published_date: string | null;
  authors: string | null;
  department: string | null;
  category: string | null;
  keywords: string | null;
  abstract: string | null;
  pdf_url: string | null;
  doi: string | null;
  summary: string | null;
}

const SITE_NAMES: Record<string, string> = {
  ntrs: 'NASA NTRS',
  mohw: '보건복지부',
};

export default function PaperList({
  initialPapers,
  initialTotal,
  siteId,
  search,
}: {
  initialPapers: Paper[];
  initialTotal: number;
  siteId?: string;
  search?: string;
}) {
  const [papers, setPapers] = useState<Paper[]>(initialPapers);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [hasMore, setHasMore] = useState(initialPapers.length < initialTotal);
  const observerRef = useRef<HTMLDivElement>(null);
  const pageSize = 20;

  // Reset when filters change
  useEffect(() => {
    setPapers(initialPapers);
    setPage(1);
    setHasMore(initialPapers.length < initialTotal);
  }, [initialPapers, initialTotal]);

  const loadMore = useCallback(async () => {
    if (loading || !hasMore) return;
    setLoading(true);

    const nextPage = page + 1;
    const params = new URLSearchParams();
    if (siteId) params.set('site', siteId);
    if (search) params.set('q', search);
    params.set('page', String(nextPage));
    params.set('pageSize', String(pageSize));

    try {
      const res = await fetch(`/api/papers?${params.toString()}`);
      const data = await res.json();

      if (data.papers.length > 0) {
        setPapers((prev) => [...prev, ...data.papers]);
        setPage(nextPage);
        setHasMore(papers.length + data.papers.length < data.total);
      } else {
        setHasMore(false);
      }
    } catch (e) {
      console.error('Failed to load more papers:', e);
    } finally {
      setLoading(false);
    }
  }, [loading, hasMore, page, siteId, search, papers.length]);

  // Intersection Observer for infinite scroll
  useEffect(() => {
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting && hasMore && !loading) {
          loadMore();
        }
      },
      { threshold: 0.1 }
    );

    if (observerRef.current) {
      observer.observe(observerRef.current);
    }

    return () => observer.disconnect();
  }, [loadMore, hasMore, loading]);

  if (papers.length === 0) {
    return (
      <div className="text-center py-12 text-gray-500">
        <p className="text-lg">검색 결과가 없습니다</p>
      </div>
    );
  }

  return (
    <>
      <p className="text-sm text-gray-500">총 {initialTotal}개의 문서</p>
      <div className="space-y-4">
        {papers.map((paper) => (
          <PaperCard
            key={paper.id}
            id={paper.id}
            title={paper.title}
            url={paper.url}
            siteName={SITE_NAMES[paper.site_id] || paper.site_id}
            siteId={paper.site_id}
            publishedDate={paper.published_date}
            authors={paper.authors}
            department={paper.department}
            category={paper.category}
            keywords={paper.keywords}
            abstract={paper.abstract}
            pdfUrl={paper.pdf_url}
            doi={paper.doi}
            hasSummary={!!paper.summary}
          />
        ))}
      </div>

      {/* Scroll sentinel */}
      <div ref={observerRef} className="py-8 text-center">
        {loading && (
          <div className="flex justify-center items-center gap-2 text-gray-500">
            <div className="w-5 h-5 border-2 border-gray-300 border-t-blue-600 rounded-full animate-spin"></div>
            <span>불러오는 중...</span>
          </div>
        )}
        {!hasMore && papers.length > 0 && (
          <p className="text-sm text-gray-400">모든 문서를 불러왔습니다</p>
        )}
      </div>
    </>
  );
}
