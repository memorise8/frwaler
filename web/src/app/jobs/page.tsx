"use client";
import useSWR from "swr";
import { fetcher } from "@/lib/api";

export default function Jobs() {
  const { data: jobs, error } = useSWR("/api/jobs", fetcher, { refreshInterval: 3000 });

  if (error) return <div className="text-red-500">Failed to load jobs</div>;
  if (!jobs) return <div className="text-gray-500">Loading...</div>;

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">Jobs</h1>

      {jobs.length === 0 ? (
        <p className="text-gray-500">No jobs yet. Start a crawl from a site page.</p>
      ) : (
        <div className="bg-white rounded-lg shadow overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b">
              <tr>
                <th className="text-left px-4 py-3 font-medium text-gray-600">ID</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Type</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Site</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Status</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Result</th>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Started</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {jobs.map((job: any) => (
                <tr key={job.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3 font-mono text-xs">{job.id}</td>
                  <td className="px-4 py-3">{job.type}</td>
                  <td className="px-4 py-3"><a href={`/sites/${job.site_id}`} className="text-blue-600 hover:underline">{job.site_id}</a></td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-0.5 rounded text-xs ${
                      job.status === "completed" ? "bg-green-100 text-green-700" :
                      job.status === "running" ? "bg-blue-100 text-blue-700" :
                      job.status === "failed" ? "bg-red-100 text-red-700" :
                      "bg-gray-100 text-gray-700"
                    }`}>{job.status}</span>
                  </td>
                  <td className="px-4 py-3 text-gray-500 text-xs">
                    {job.result || "-"}
                    {job.diagnosis && job.diagnosis.length > 0 && (
                      <div className="mt-1 flex flex-col gap-0.5">
                        {job.diagnosis.map((hint: string, i: number) => {
                          const type = hint.startsWith("JS_RENDERING") ? "JS_RENDERING"
                            : hint.startsWith("IFRAME") ? "IFRAME"
                            : hint.startsWith("SELECTOR") ? "SELECTOR"
                            : hint.startsWith("LINKS") ? "LINKS"
                            : "OTHER";
                          const colorClass = type === "JS_RENDERING" ? "bg-blue-100 text-blue-700"
                            : type === "IFRAME" ? "bg-purple-100 text-purple-700"
                            : type === "SELECTOR" ? "bg-orange-100 text-orange-700"
                            : "bg-gray-100 text-gray-600";
                          return (
                            <span key={i} className={`inline-block px-1.5 py-0.5 rounded text-xs leading-tight ${colorClass}`}>
                              {hint}
                            </span>
                          );
                        })}
                      </div>
                    )}
                  </td>
                  <td className="px-4 py-3 text-gray-500 text-xs whitespace-nowrap">{job.started_at?.replace("T", " ").slice(0, 19)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
