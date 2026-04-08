"use client";
import { Suspense, useState } from "react";
import useSWR from "swr";
import { postApi, fetcher } from "@/lib/api";
import { useSearchParams } from "next/navigation";

function AutoAddContent() {
  const searchParams = useSearchParams();
  const [url, setUrl] = useState(searchParams.get("url") || "");
  const [siteId, setSiteId] = useState("");
  const [browser, setBrowser] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  // Poll job status when jobId is set
  const { data: job } = useSWR(
    jobId ? `/api/jobs/${jobId}` : null,
    fetcher,
    { refreshInterval: 2000 }
  );

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setSubmitting(true);
    try {
      const res = await postApi("/api/auto-add", { url, site_id: siteId || undefined, browser });
      setJobId(res.id);
    } catch (err: any) {
      setError(err.message || "Failed to start auto-add");
    } finally {
      setSubmitting(false);
    }
  };

  const isRunning = job && job.status === "running";
  const isComplete = job && job.status === "completed";
  const isFailed = job && job.status === "failed";

  return (
    <div>
      <h1 className="text-2xl font-bold mb-2">사이트 자동 추가</h1>
      <p className="text-sm text-gray-500 mb-6">URL을 입력하면 AI 에이전트가 페이지 구조를 분석하여 크롤러 설정을 자동으로 생성합니다.</p>

      <form onSubmit={handleSubmit} className="bg-white rounded-lg shadow p-6 mb-6 space-y-4">
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">분석할 URL</label>
          <input type="url" required value={url} onChange={(e) => setUrl(e.target.value)}
            placeholder="https://example.com/publications"
            className="w-full px-4 py-2 border rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" />
        </div>
        <div className="flex gap-4">
          <div className="flex-1">
            <label className="block text-sm font-medium text-gray-700 mb-1">사이트 ID (선택사항)</label>
            <input type="text" value={siteId} onChange={(e) => setSiteId(e.target.value)}
              placeholder="비워두면 자동 생성"
              className="w-full px-3 py-2 border rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" />
          </div>
          <div className="flex items-end pb-1">
            <label className="flex items-center gap-2 text-sm cursor-pointer">
              <input type="checkbox" checked={browser} onChange={(e) => setBrowser(e.target.checked)}
                className="w-4 h-4 rounded border-gray-300" />
              브라우저 사용 (SPA/React 사이트용)
            </label>
          </div>
        </div>
        <button type="submit" disabled={submitting || isRunning}
          className="bg-purple-600 text-white px-6 py-2 rounded-lg hover:bg-purple-700 text-sm disabled:opacity-50">
          {submitting ? "시작 중..." : "분석 및 추가"}
        </button>
      </form>

      {error && <div className="bg-red-50 border border-red-200 rounded-lg p-4 mb-4 text-red-700">{error}</div>}

      {job && (
        <div className={`rounded-lg shadow p-6 ${isComplete ? "bg-green-50 border border-green-200" : isFailed ? "bg-red-50 border border-red-200" : "bg-blue-50 border border-blue-200"}`}>
          <div className="flex items-center gap-3 mb-3">
            {isRunning && <div className="animate-spin h-5 w-5 border-2 border-blue-500 border-t-transparent rounded-full"></div>}
            <h3 className="font-semibold text-lg">
              {isRunning ? "분석 중..." : isComplete ? "성공" : "실패"}
            </h3>
            <span className="text-xs text-gray-500">Job: {job.id}</span>
          </div>
          {isRunning && <p className="text-sm text-gray-600">{job.progress || "AI 에이전트가 페이지 구조를 분석하고 있습니다..."}</p>}
          {(isComplete || isFailed) && (
            <p className="text-sm">{job.result}</p>
          )}
          {isComplete && (
            <a href="/sites" className="inline-block mt-3 text-blue-600 hover:underline text-sm">사이트 목록 보기 &rarr;</a>
          )}
        </div>
      )}

      <div className="mt-8 bg-gray-50 rounded-lg p-4 text-sm text-gray-600">
        <h3 className="font-medium mb-2">작동 방식</h3>
        <ol className="list-decimal list-inside space-y-1">
          <li>AI 에이전트가 페이지 HTML 구조를 가져와 분석합니다</li>
          <li>문서 링크, 제목, 날짜, 카테고리를 식별합니다</li>
          <li>CSS 셀렉터를 테스트하고 검증합니다</li>
          <li>크롤러 JSON 설정이 자동으로 생성 및 저장됩니다</li>
          <li>테스트 크롤링으로 설정이 올바르게 작동하는지 확인합니다</li>
        </ol>
        <p className="mt-3 text-xs text-gray-400">OPENAI_API_KEY 환경변수 설정이 필요합니다.</p>
      </div>
    </div>
  );
}

export default function AutoAdd() {
  return (
    <Suspense fallback={<div className="p-4 text-sm text-gray-500">Loading...</div>}>
      <AutoAddContent />
    </Suspense>
  );
}
