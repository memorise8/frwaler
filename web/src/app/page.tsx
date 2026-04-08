"use client";
import useSWR from "swr";
import { fetcher } from "@/lib/api";

export default function Dashboard() {
  const { data: stats, error } = useSWR("/api/stats", fetcher, { refreshInterval: 10000 });

  if (error) return <div className="text-red-500">Failed to load stats</div>;
  if (!stats) return <div className="text-gray-500">Loading...</div>;

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">Dashboard</h1>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-8">
        <div className="bg-white rounded-lg shadow p-6">
          <p className="text-sm text-gray-500">Total Sites</p>
          <p className="text-3xl font-bold">{stats.total_sites}</p>
        </div>
        <div className="bg-white rounded-lg shadow p-6">
          <p className="text-sm text-gray-500">Total Papers</p>
          <p className="text-3xl font-bold">{stats.total_papers.toLocaleString()}</p>
        </div>
        <div className="bg-white rounded-lg shadow p-6">
          <p className="text-sm text-gray-500">Downloaded</p>
          <p className="text-3xl font-bold">
            {stats.sites.reduce((sum: number, s: any) => sum + (s.downloaded || 0), 0).toLocaleString()}
          </p>
        </div>
      </div>

      <h2 className="text-lg font-semibold mb-3">Sites Overview</h2>
      <div className="bg-white rounded-lg shadow overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 border-b">
            <tr>
              <th className="text-left px-4 py-3 font-medium text-gray-600">Site</th>
              <th className="text-right px-4 py-3 font-medium text-gray-600">Papers</th>
              <th className="text-right px-4 py-3 font-medium text-gray-600">Downloaded</th>
              <th className="text-right px-4 py-3 font-medium text-gray-600">Summarized</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {stats.sites.filter((s: any) => s.paper_count > 0).map((site: any) => (
              <tr key={site.id} className="hover:bg-gray-50">
                <td className="px-4 py-3">
                  <a href={`/sites/${site.id}`} className="text-blue-600 hover:underline font-medium">{site.name}</a>
                  <p className="text-xs text-gray-400">{site.id}</p>
                </td>
                <td className="text-right px-4 py-3">{site.paper_count.toLocaleString()}</td>
                <td className="text-right px-4 py-3">{(site.downloaded || 0).toLocaleString()}</td>
                <td className="text-right px-4 py-3">{(site.summarized || 0).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
