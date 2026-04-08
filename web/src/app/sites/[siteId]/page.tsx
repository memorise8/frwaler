"use client";
import { useParams, useSearchParams } from "next/navigation";
import Link from "next/link";
import useSWR from "swr";
import { fetcher, postApi } from "@/lib/api";
import { useState } from "react";

export default function SitePapers() {
  const params = useParams();
  const searchParams = useSearchParams();
  const siteId = params.siteId as string;
  const page = parseInt(searchParams.get("page") || "1");
  const [crawling, setCrawling] = useState(false);
  const [crawlLimit, setCrawlLimit] = useState(10);
  const [jobId, setJobId] = useState<string | null>(null);

  const { data, error } = useSWR(`/api/sites/${siteId}/papers?page=${page}&limit=20`, fetcher);

  const handleCrawl = async () => {
    setCrawling(true);
    setJobId(null);
    try {
      const job = await postApi(`/api/crawl/${siteId}`, { limit: crawlLimit });
      setJobId(job.id);
    } catch (e: any) {
      alert(`Error: ${e.message}`);
    }
    setCrawling(false);
  };

  if (error) return <div className="text-red-500">Failed to load papers</div>;
  if (!data) return <div className="text-gray-500">Loading...</div>;

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold">{siteId}</h1>
          <p className="text-sm text-gray-500">{data.total} papers total</p>
        </div>
        <div className="flex flex-col items-end gap-2">
          <div className="flex items-center gap-2">
            <select
              value={crawlLimit}
              onChange={(e) => setCrawlLimit(Number(e.target.value))}
              className="border rounded-lg px-2 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-300"
              disabled={crawling}
            >
              <option value={10}>10 items</option>
              <option value={20}>20 items</option>
              <option value={50}>50 items</option>
              <option value={100}>100 items</option>
            </select>
            <button
              onClick={handleCrawl}
              disabled={crawling}
              className="bg-blue-600 text-white px-4 py-2 rounded-lg hover:bg-blue-700 disabled:opacity-50 text-sm"
            >
              {crawling ? "Starting..." : "Crawl"}
            </button>
            <Link href={`/sites/${siteId}/config`} className="bg-gray-100 text-gray-700 px-4 py-2 rounded-lg hover:bg-gray-200 text-sm">
              설정 편집
            </Link>
          </div>
          {jobId && (
            <p className="text-sm text-gray-500">
              Job <span className="font-mono text-xs">{jobId}</span> started —{" "}
              <a href="/jobs" className="text-blue-600 hover:underline">View Jobs</a>
            </p>
          )}
        </div>
      </div>

      <div className="bg-white rounded-lg shadow overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 border-b">
            <tr>
              <th className="text-left px-4 py-3 font-medium text-gray-600">Title</th>
              <th className="text-left px-4 py-3 font-medium text-gray-600">Date</th>
              <th className="text-left px-4 py-3 font-medium text-gray-600">Status</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {data.items.map((paper: any) => (
              <tr key={paper.id} className="hover:bg-gray-50">
                <td className="px-4 py-3">
                  <a href={paper.url} target="_blank" rel="noopener" className="text-blue-600 hover:underline">
                    {paper.title || "(no title)"}
                  </a>
                  {paper.category && <span className="ml-2 text-xs bg-gray-100 px-2 py-0.5 rounded">{paper.category}</span>}
                </td>
                <td className="px-4 py-3 text-gray-500 whitespace-nowrap">{paper.published_date || paper.crawled_at?.split("T")[0] || "-"}</td>
                <td className="px-4 py-3">
                  {paper.download_status === "downloaded" ? (
                    <span className="text-green-600 text-xs">Downloaded</span>
                  ) : paper.pdf_url ? (
                    <a href={paper.pdf_url} target="_blank" className="text-blue-500 text-xs hover:underline">PDF</a>
                  ) : (
                    <span className="text-gray-400 text-xs">-</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="flex gap-2 mt-4 justify-center">
        {page > 1 && <a href={`/sites/${siteId}?page=${page - 1}`} className="px-3 py-1 bg-white border rounded text-sm hover:bg-gray-50">← Prev</a>}
        <span className="px-3 py-1 text-sm text-gray-500">Page {page} of {Math.ceil(data.total / 20)}</span>
        {page * 20 < data.total && <a href={`/sites/${siteId}?page=${page + 1}`} className="px-3 py-1 bg-white border rounded text-sm hover:bg-gray-50">Next →</a>}
      </div>
    </div>
  );
}
