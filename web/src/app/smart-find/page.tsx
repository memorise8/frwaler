"use client";
import { useState } from "react";
import useSWR from "swr";
import { postApi, fetcher } from "@/lib/api";

interface FoundFile {
  id: string;
  title: string;
  file_url: string;
  file_type: string;
  source_page: string;
}

export default function SmartFind() {
  const [url, setUrl] = useState("");
  const [maxPages, setMaxPages] = useState(20);
  const [maxDepth, setMaxDepth] = useState(3);
  const [useAi, setUseAi] = useState(true);
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [downloading, setDownloading] = useState(false);

  // Poll job status
  const { data: job } = useSWR(
    jobId ? `/api/jobs/${jobId}` : null,
    fetcher,
    { refreshInterval: 2000 }
  );

  // Get found files when job completes
  const { data: filesData } = useSWR(
    job && job.status === "completed" ? `/api/smart-find/${jobId}/files` : null,
    fetcher
  );

  const handleFind = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setSubmitting(true);
    setJobId(null);
    try {
      const res = await postApi("/api/smart-find", { url, max_pages: maxPages, max_depth: maxDepth, use_ai: useAi });
      setJobId(res.id);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  };

  const handleDownload = async () => {
    if (!jobId) return;
    setDownloading(true);
    try {
      await postApi(`/api/smart-find/${jobId}/download`, {});
    } catch {}
    setDownloading(false);
  };

  const isRunning = job && job.status === "running";
  const isComplete = job && job.status === "completed";
  const isFailed = job && job.status === "failed";

  const fileTypeBadge = (type: string) => {
    const colors: Record<string, string> = {
      pdf: "bg-red-100 text-red-700",
      hwp: "bg-blue-100 text-blue-700",
      hwpx: "bg-blue-100 text-blue-700",
      xlsx: "bg-green-100 text-green-700",
      xls: "bg-green-100 text-green-700",
      csv: "bg-green-100 text-green-700",
      docx: "bg-indigo-100 text-indigo-700",
      doc: "bg-indigo-100 text-indigo-700",
      pptx: "bg-orange-100 text-orange-700",
      zip: "bg-gray-100 text-gray-700",
      unknown: "bg-gray-100 text-gray-500",
    };
    return colors[type] || colors.unknown;
  };

  const files: FoundFile[] = filesData?.files || [];

  // Count by type
  const typeCounts: Record<string, number> = {};
  files.forEach(f => {
    typeCounts[f.file_type] = (typeCounts[f.file_type] || 0) + 1;
  });

  return (
    <div>
      <h1 className="text-2xl font-bold mb-2">문서 찾기</h1>
      <p className="text-sm text-gray-500 mb-6">URL을 입력하면 해당 페이지에서 다운로드 가능한 모든 문서를 자동으로 찾습니다.</p>

      <form onSubmit={handleFind} className="bg-white rounded-lg shadow p-6 mb-6 space-y-4">
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">URL</label>
          <input type="url" required value={url} onChange={(e) => setUrl(e.target.value)}
            placeholder="https://www.example.go.kr/board/list.do"
            className="w-full px-4 py-2 border rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" />
        </div>
        <div className="flex gap-6 items-center">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">최대 페이지 수: {maxPages}</label>
            <input type="range" min={1} max={50} value={maxPages} onChange={(e) => setMaxPages(Number(e.target.value))}
              className="w-48" />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">탐색 깊이: {maxDepth}</label>
            <input type="range" min={1} max={5} value={maxDepth} onChange={(e) => setMaxDepth(Number(e.target.value))}
              className="w-48" />
          </div>
          <label className="flex items-center gap-2 text-sm cursor-pointer">
            <input type="checkbox" checked={useAi} onChange={(e) => setUseAi(e.target.checked)} className="w-4 h-4" />
            AI 보조 분석
          </label>
        </div>
        <button type="submit" disabled={submitting || isRunning}
          className="bg-blue-600 text-white px-6 py-2 rounded-lg hover:bg-blue-700 text-sm disabled:opacity-50">
          {submitting ? "시작 중..." : "문서 찾기"}
        </button>
      </form>

      {error && <div className="bg-red-50 border border-red-200 rounded-lg p-4 mb-4 text-red-700">{error}</div>}

      {/* Progress */}
      {isRunning && (
        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 mb-4">
          <div className="flex items-center gap-3">
            <div className="animate-spin h-5 w-5 border-2 border-blue-500 border-t-transparent rounded-full"></div>
            <span className="font-medium">탐색 중...</span>
          </div>
          <p className="text-sm text-blue-700 mt-2">{job.progress || "페이지를 분석하고 있습니다..."}</p>
        </div>
      )}

      {isFailed && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-4 mb-4">
          <p className="text-red-700 font-medium">탐색 실패</p>
          <p className="text-sm text-red-600">{job.result}</p>
        </div>
      )}

      {/* Results */}
      {isComplete && (
        <div>
          <div className="bg-green-50 border border-green-200 rounded-lg p-4 mb-4">
            <p className="font-medium text-green-800">{job.result}</p>
            {Object.keys(typeCounts).length > 0 && (
              <div className="flex gap-2 mt-2 flex-wrap">
                {Object.entries(typeCounts).sort((a,b) => b[1]-a[1]).map(([type, count]) => (
                  <span key={type} className={`px-2 py-0.5 rounded text-xs font-medium ${fileTypeBadge(type)}`}>
                    {type.toUpperCase()} {count}
                  </span>
                ))}
              </div>
            )}
          </div>

          {files.length > 0 && (
            <>
              <div className="flex justify-between items-center mb-3">
                <h3 className="font-medium">발견된 파일 ({files.length}개)</h3>
                <button onClick={handleDownload} disabled={downloading}
                  className="bg-purple-600 text-white px-4 py-1.5 rounded text-sm hover:bg-purple-700 disabled:opacity-50">
                  {downloading ? "다운로드 중..." : "전체 다운로드"}
                </button>
              </div>
              <div className="bg-white rounded-lg shadow overflow-hidden">
                <table className="w-full text-sm">
                  <thead className="bg-gray-50 border-b">
                    <tr>
                      <th className="text-left px-4 py-3 font-medium text-gray-600">#</th>
                      <th className="text-left px-4 py-3 font-medium text-gray-600">파일명</th>
                      <th className="text-center px-4 py-3 font-medium text-gray-600">타입</th>
                      <th className="text-left px-4 py-3 font-medium text-gray-600">출처</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y">
                    {files.map((file, i) => (
                      <tr key={file.id} className="hover:bg-gray-50">
                        <td className="px-4 py-2 text-gray-400">{i + 1}</td>
                        <td className="px-4 py-2">
                          <a href={file.file_url} target="_blank" rel="noopener" className="text-blue-600 hover:underline">
                            {file.title || "Untitled"}
                          </a>
                        </td>
                        <td className="px-4 py-2 text-center">
                          <span className={`px-2 py-0.5 rounded text-xs font-medium ${fileTypeBadge(file.file_type)}`}>
                            {file.file_type.toUpperCase()}
                          </span>
                        </td>
                        <td className="px-4 py-2 text-xs text-gray-500 truncate max-w-xs">
                          {(() => { try { return new URL(file.source_page).pathname.slice(0, 50); } catch { return file.source_page?.slice(0, 50) || ""; } })()}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
