"use client";

import { useSearchParams } from "next/navigation";
import { useEffect, useRef, useState, Suspense } from "react";

interface Job {
  jobId: string;
  pid: number;
  siteId: string;
  logPath: string;
  startedAt: string;
}

interface LogFile {
  name: string;
  size: number;
  mtime: string;
}

// doc_type is an optional site-specific filter passed through to
// `crawler.main`. The free-form text input below lets operators target
// site-specific categories (e.g. an NTS doc code) without baking any
// legacy tax-law presets into the UI.

function CrawlerPageInner() {
  const searchParams = useSearchParams();
  const urlSite = searchParams.get("site");
  const fromAutoAdd = searchParams.get("from") === "auto-add";

  const [jobs, setJobs] = useState<Job[]>([]);
  const [logs, setLogs] = useState<LogFile[]>([]);
  const [sites, setSites] = useState<string[]>([]);
  const [siteId, setSiteId] = useState<string>(urlSite || "");
  const [docType, setDocType] = useState("");
  const [incremental, setIncremental] = useState(true);
  const [limit, setLimit] = useState<number | "">("");

  const [logFile, setLogFile] = useState<string | null>(null);
  const [logLines, setLogLines] = useState<string[]>([]);
  const logEsRef = useRef<EventSource | null>(null);
  const logPaneRef = useRef<HTMLDivElement>(null);

  const inputClass =
    "px-3 py-2 border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 text-slate-900 dark:text-slate-100 rounded-lg text-sm";

  const refreshState = async () => {
    const res = await fetch("/api/crawler", { cache: "no-store" });
    const data = await res.json();
    setJobs(data.jobs ?? []);
    setLogs(data.logs ?? []);
  };

  useEffect(() => {
    fetch("/api/crawler/sites")
      .then((r) => r.json())
      .then((d) => {
        const list: string[] = d.sites ?? [];
        setSites(list);
        // If we have no site selected yet and the URL didn't pre-pick one,
        // adopt the first available site so the dropdown is never blank.
        setSiteId((prev) => (prev ? prev : list[0] ?? ""));
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    const initial = setTimeout(() => {
      void refreshState();
    }, 0);
    const iv = setInterval(refreshState, 5000);
    return () => {
      clearTimeout(initial);
      clearInterval(iv);
    };
  }, []);

  useEffect(() => {
    if (!logFile) return;
    const es = new EventSource(
      `/api/crawler/logs?file=${encodeURIComponent(logFile)}`
    );
    logEsRef.current = es;
    es.onmessage = (e) => {
      setLogLines((prev) => {
        const next = [...prev, e.data];
        if (next.length > 500) return next.slice(-500);
        return next;
      });
    };
    es.onerror = () => {
      es.close();
    };
    return () => {
      es.close();
      logEsRef.current = null;
    };
  }, [logFile]);

  useEffect(() => {
    if (logPaneRef.current) {
      logPaneRef.current.scrollTop = logPaneRef.current.scrollHeight;
    }
  }, [logLines]);

  const start = async () => {
    const res = await fetch("/api/crawler", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        action: "start",
        siteId,
        docType: docType || undefined,
        incremental,
        limit: limit || undefined,
      }),
    });
    const data = await res.json();
    if (data.error) {
      alert(`오류: ${data.error}`);
      return;
    }
    await refreshState();
    const name = data.job?.logPath?.split("/").pop();
    if (name) {
      setLogLines([]);
      setLogFile(name);
    }
  };

  const stop = async (jobId: string) => {
    if (!confirm(`정말 ${jobId} 를 중지하시겠습니까?`)) return;
    await fetch("/api/crawler", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ action: "stop", jobId }),
    });
    await refreshState();
  };

  // Merge sites list with current siteId to ensure it always appears in dropdown
  const selectOptions = Array.from(new Set([...sites, siteId])).sort();

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">크롤러 관리</h1>
        <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
          크롤러 수동 실행 · 진행 로그 실시간 스트리밍
        </p>
      </div>

      <section className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 p-5 space-y-4">
        <h2 className="text-sm font-semibold text-slate-700 dark:text-slate-300">
          새 크롤링 실행
        </h2>

        {fromAutoAdd && (
          <div className="rounded-lg border border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-950/40 px-4 py-3 text-sm text-amber-800 dark:text-amber-200">
            Auto-Add에서 생성된 사이트입니다. 먼저{" "}
            <code className="font-mono text-xs bg-amber-100 dark:bg-amber-900/50 px-1 py-0.5 rounded">
              --limit 5
            </code>{" "}
            정도로 검증 실행을 권장합니다.
          </div>
        )}

        <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
          <div>
            <label className="text-xs font-semibold text-slate-600 dark:text-slate-300 mb-1 block">
              사이트
            </label>
            <select
              value={siteId}
              onChange={(e) => setSiteId(e.target.value)}
              className={`w-full ${inputClass}`}
            >
              {selectOptions.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="text-xs font-semibold text-slate-600 dark:text-slate-300 mb-1 block">
              doc_type (선택)
            </label>
            <input
              type="text"
              value={docType}
              onChange={(e) => setDocType(e.target.value)}
              placeholder="사이트별 코드"
              className={`w-full ${inputClass}`}
            />
          </div>
          <div>
            <label className="text-xs font-semibold text-slate-600 dark:text-slate-300 mb-1 block">
              limit (선택)
            </label>
            <input
              type="number"
              min={1}
              value={limit}
              onChange={(e) =>
                setLimit(e.target.value ? parseInt(e.target.value, 10) : "")
              }
              placeholder="무제한"
              className={`w-full ${inputClass}`}
            />
          </div>
          <div className="flex items-end pb-1">
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={incremental}
                onChange={(e) => setIncremental(e.target.checked)}
              />
              증분 (incremental)
            </label>
          </div>
          <div className="flex items-end">
            <button
              onClick={start}
              className={`w-full px-4 py-2 text-white rounded-lg text-sm font-medium transition-colors ${
                fromAutoAdd
                  ? "bg-amber-600 hover:bg-amber-700 ring-2 ring-amber-400 dark:ring-amber-500"
                  : "bg-slate-900 dark:bg-slate-700 hover:bg-slate-800 dark:hover:bg-slate-600"
              }`}
            >
              실행
            </button>
          </div>
        </div>
      </section>

      <section>
        <h2 className="text-lg font-semibold mb-3">
          실행 중 Job ({jobs.length})
        </h2>
        <div className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 overflow-hidden">
          {jobs.length === 0 ? (
            <div className="px-6 py-10 text-center text-slate-400 dark:text-slate-500 text-sm">
              실행 중인 job이 없습니다.
            </div>
          ) : (
            <table className="w-full text-sm">
              <thead className="bg-slate-50 dark:bg-slate-800/50 text-slate-600 dark:text-slate-300">
                <tr>
                  <th className="text-left px-4 py-2.5 font-medium">Job ID</th>
                  <th className="text-left px-4 py-2.5 font-medium">PID</th>
                  <th className="text-left px-4 py-2.5 font-medium">시작</th>
                  <th className="text-right px-4 py-2.5 font-medium">동작</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                {jobs.map((j) => {
                  const logName = j.logPath.split("/").pop() ?? "";
                  return (
                    <tr key={j.jobId}>
                      <td className="px-4 py-2 font-mono text-xs">{j.jobId}</td>
                      <td className="px-4 py-2 font-mono">{j.pid}</td>
                      <td className="px-4 py-2 text-slate-500 dark:text-slate-400">
                        {new Date(j.startedAt).toLocaleString("ko-KR")}
                      </td>
                      <td className="px-4 py-2 text-right">
                        <button
                          onClick={() => setLogFile(logName)}
                          className="text-blue-600 dark:text-blue-400 hover:underline text-xs mr-3"
                        >
                          로그 보기
                        </button>
                        <button
                          onClick={() => stop(j.jobId)}
                          className="text-red-600 dark:text-red-400 hover:underline text-xs"
                        >
                          중지
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </section>

      <section>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold">로그 파일 ({logs.length})</h2>
          {logFile && (
            <button
              onClick={() => {
                setLogFile(null);
                logEsRef.current?.close();
              }}
              className="text-xs text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-100"
            >
              스트림 닫기
            </button>
          )}
        </div>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 overflow-y-auto max-h-[500px]">
            <ul className="divide-y divide-slate-100 dark:divide-slate-800">
              {logs.map((l) => (
                <li key={l.name}>
                  <button
                    onClick={() => setLogFile(l.name)}
                    className={`w-full text-left px-4 py-2 text-sm hover:bg-slate-50 dark:hover:bg-slate-800/50 ${
                      logFile === l.name
                        ? "bg-blue-50 dark:bg-blue-950/40 text-blue-900 dark:text-blue-200 font-medium"
                        : ""
                    }`}
                  >
                    <div className="font-mono text-xs truncate">{l.name}</div>
                    <div className="text-xs text-slate-400 dark:text-slate-500 mt-0.5">
                      {(l.size / 1024).toFixed(1)} KB ·{" "}
                      {new Date(l.mtime).toLocaleString("ko-KR")}
                    </div>
                  </button>
                </li>
              ))}
              {logs.length === 0 && (
                <li className="px-4 py-6 text-center text-slate-400 dark:text-slate-500 text-sm">
                  로그 파일 없음
                </li>
              )}
            </ul>
          </div>
          <div className="md:col-span-2 bg-slate-950 dark:bg-black rounded-xl border border-slate-800 overflow-hidden flex flex-col">
            <div className="bg-slate-900 px-4 py-2 text-xs font-mono text-slate-300 border-b border-slate-800">
              {logFile ?? "로그 파일을 선택하세요"}
            </div>
            <div
              ref={logPaneRef}
              className="flex-1 overflow-y-auto p-4 font-mono text-xs text-emerald-300 max-h-[450px] min-h-[200px]"
            >
              {logLines.length === 0 ? (
                <div className="text-slate-500">
                  {logFile ? "스트림 연결 중..." : "선택된 로그가 없습니다."}
                </div>
              ) : (
                logLines.map((line, i) => (
                  <div key={i} className="whitespace-pre-wrap break-all">
                    {line}
                  </div>
                ))
              )}
            </div>
          </div>
        </div>
      </section>
    </div>
  );
}

export default function CrawlerPage() {
  return (
    <Suspense fallback={<div className="p-6 text-slate-500 text-sm">로딩 중...</div>}>
      <CrawlerPageInner />
    </Suspense>
  );
}
