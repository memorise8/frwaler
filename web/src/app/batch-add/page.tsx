"use client";
import { useState, useRef } from "react";
import { postApi, fetcher } from "@/lib/api";

interface BatchItem {
  url: string;
  status: "pending" | "running" | "success" | "failed";
  jobId?: string;
  result?: string;
}

export default function BatchAdd() {
  const [input, setInput] = useState("");
  const [browser, setBrowser] = useState(false);
  const [items, setItems] = useState<BatchItem[]>([]);
  const [running, setRunning] = useState(false);
  const stopRef = useRef(false);

  const handleStart = async () => {
    const urls = input
      .split("\n")
      .map((l) => l.trim())
      .filter((l) => l && !l.startsWith("#") && l.startsWith("http"));

    if (urls.length === 0) return;

    const batch: BatchItem[] = urls.map((url) => ({ url, status: "pending" }));
    setItems(batch);
    setRunning(true);
    stopRef.current = false;

    for (let i = 0; i < batch.length; i++) {
      if (stopRef.current) break;

      // Update status to running
      batch[i].status = "running";
      setItems([...batch]);

      try {
        const res = await postApi("/api/auto-add", {
          url: batch[i].url,
          browser,
        });
        batch[i].jobId = res.id;

        // Poll job status
        let done = false;
        while (!done && !stopRef.current) {
          await new Promise((r) => setTimeout(r, 3000));
          try {
            const job = await fetcher(`/api/jobs/${res.id}`);
            if (job.status === "completed") {
              batch[i].status = "success";
              batch[i].result = job.result;
              done = true;
            } else if (job.status === "failed") {
              batch[i].status = "failed";
              batch[i].result = job.result;
              done = true;
            }
            // still running, continue polling
          } catch {
            done = true;
            batch[i].status = "failed";
            batch[i].result = "상태 확인 실패";
          }
        }
      } catch (err: unknown) {
        batch[i].status = "failed";
        batch[i].result =
          err instanceof Error ? err.message : "알 수 없는 오류";
      }

      setItems([...batch]);
    }
    setRunning(false);
  };

  const handleStop = () => {
    stopRef.current = true;
  };

  const summary = {
    total: items.length,
    success: items.filter((i) => i.status === "success").length,
    failed: items.filter((i) => i.status === "failed").length,
    pending: items.filter((i) => i.status === "pending").length,
    running: items.filter((i) => i.status === "running").length,
  };

  const urlCount = input
    .split("\n")
    .filter((l) => l.trim() && !l.startsWith("#") && l.trim().startsWith("http")).length;

  return (
    <div>
      <h1 className="text-2xl font-bold mb-2">일괄 사이트 추가</h1>
      <p className="text-sm text-gray-500 mb-6">
        여러 URL을 한 번에 붙여넣어 자동으로 크롤러 설정을 생성합니다.
      </p>

      {!running && items.length === 0 && (
        <div className="bg-white rounded-lg shadow p-6 mb-6 space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              URL 목록 (한 줄에 하나씩)
            </label>
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder={
                "https://www.example.com/board/list.do\nhttps://www.example2.go.kr/news/press\n# 주석은 #으로 시작"
              }
              rows={12}
              className="w-full px-4 py-3 border rounded-lg text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <div className="flex items-center gap-4">
            <label className="flex items-center gap-2 text-sm cursor-pointer">
              <input
                type="checkbox"
                checked={browser}
                onChange={(e) => setBrowser(e.target.checked)}
                className="w-4 h-4"
              />
              브라우저 모드 (SPA/JS 사이트)
            </label>
            <span className="text-xs text-gray-400">
              {urlCount}개 URL 감지됨
            </span>
          </div>
          <button
            onClick={handleStart}
            disabled={urlCount === 0}
            className="bg-purple-600 text-white px-6 py-2 rounded-lg hover:bg-purple-700 text-sm disabled:opacity-50"
          >
            분석 시작
          </button>
        </div>
      )}

      {items.length > 0 && (
        <>
          {/* Summary bar */}
          <div className="bg-white rounded-lg shadow p-4 mb-4 flex items-center justify-between">
            <div className="flex gap-4 text-sm">
              <span>
                총 <strong>{summary.total}</strong>개
              </span>
              <span className="text-green-600">
                성공 <strong>{summary.success}</strong>
              </span>
              <span className="text-red-600">
                실패 <strong>{summary.failed}</strong>
              </span>
              <span className="text-blue-600">
                진행 중 <strong>{summary.running}</strong>
              </span>
              <span className="text-gray-500">
                대기 <strong>{summary.pending}</strong>
              </span>
            </div>
            {running ? (
              <button
                onClick={handleStop}
                className="bg-red-500 text-white px-4 py-1.5 rounded text-sm hover:bg-red-600"
              >
                중단
              </button>
            ) : (
              <button
                onClick={() => {
                  setItems([]);
                }}
                className="bg-gray-200 text-gray-700 px-4 py-1.5 rounded text-sm hover:bg-gray-300"
              >
                초기화
              </button>
            )}
          </div>

          {/* Results table */}
          <div className="bg-white rounded-lg shadow overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 border-b">
                <tr>
                  <th className="text-left px-4 py-3 font-medium text-gray-600 w-12">
                    #
                  </th>
                  <th className="text-left px-4 py-3 font-medium text-gray-600">
                    URL
                  </th>
                  <th className="text-center px-4 py-3 font-medium text-gray-600 w-24">
                    상태
                  </th>
                  <th className="text-left px-4 py-3 font-medium text-gray-600">
                    결과
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {items.map((item, i) => (
                  <tr key={i} className="hover:bg-gray-50">
                    <td className="px-4 py-2 text-gray-400">{i + 1}</td>
                    <td className="px-4 py-2 font-mono text-xs truncate max-w-md">
                      {item.url}
                    </td>
                    <td className="px-4 py-2 text-center">
                      <span
                        className={`px-2 py-0.5 rounded-full text-xs font-medium ${
                          item.status === "pending"
                            ? "bg-gray-100 text-gray-600"
                            : item.status === "running"
                              ? "bg-blue-100 text-blue-700"
                              : item.status === "success"
                                ? "bg-green-100 text-green-700"
                                : "bg-red-100 text-red-700"
                        }`}
                      >
                        {item.status === "pending"
                          ? "대기"
                          : item.status === "running"
                            ? "분석 중"
                            : item.status === "success"
                              ? "성공"
                              : "실패"}
                      </span>
                    </td>
                    <td className="px-4 py-2 text-xs text-gray-600 truncate max-w-xs">
                      {item.result ||
                        (item.status === "running"
                          ? "AI 에이전트 분석 중..."
                          : "")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
