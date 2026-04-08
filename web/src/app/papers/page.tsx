"use client";
import { useState } from "react";
import useSWR from "swr";
import { fetcher } from "@/lib/api";

export default function Papers() {
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const { data, error } = useSWR(search ? `/api/papers?q=${encodeURIComponent(search)}&limit=30` : null, fetcher);

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">Search Papers</h1>

      <form onSubmit={(e) => { e.preventDefault(); setSearch(query); }} className="flex gap-2 mb-6">
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search papers by title or abstract..."
          className="flex-1 px-4 py-2 border rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
        <button type="submit" className="bg-blue-600 text-white px-6 py-2 rounded-lg hover:bg-blue-700 text-sm">Search</button>
      </form>

      {error && <div className="text-red-500">Search failed</div>}
      {data && (
        <div>
          <p className="text-sm text-gray-500 mb-3">{data.total} results</p>
          <div className="space-y-3">
            {data.items.map((paper: any) => (
              <div key={paper.id} className="bg-white rounded-lg shadow p-4">
                <a href={paper.url} target="_blank" rel="noopener" className="text-blue-600 hover:underline font-medium">
                  {paper.title || "(no title)"}
                </a>
                <div className="flex gap-3 text-xs text-gray-500 mt-1">
                  <span>{paper.site_id}</span>
                  {paper.published_date && <span>{paper.published_date}</span>}
                  {paper.category && <span className="bg-gray-100 px-2 py-0.5 rounded">{paper.category}</span>}
                </div>
                {paper.abstract && <p className="text-sm text-gray-600 mt-2 line-clamp-2">{paper.abstract}</p>}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
