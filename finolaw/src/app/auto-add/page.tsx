"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";

interface CodexJob {
  jobId: string;
  pid: number;
  url: string;
  siteId: string | null;
  siteName: string | null;
  logPath: string;
  startedAt: string;
}

interface ResultEvent {
  success: boolean;
  site_id?: string;
  file_path?: string;
  quality?: string;
  count?: number;
  elapsed_seconds?: number;
  error?: string;
  reason?: string;
  url?: string;
  first_title?: string;
}

type Phase = "analyzing" | "building" | "verifying" | "saving" | "done" | "failed";

interface PhaseInfo {
  key: Phase;
  label: string;
}

const PHASES: PhaseInfo[] = [
  { key: "analyzing", label: "사이트 구조 분석" },
  { key: "building",  label: "전용 크롤러 코드 작성" },
  { key: "verifying", label: "크롤러 검증" },
  { key: "saving",    label: "저장" },
];

const PHASE_ORDER: Phase[] = ["analyzing", "building", "verifying", "saving", "done", "failed"];

function phaseIndex(p: Phase): number {
  return PHASE_ORDER.indexOf(p);
}

function translateError(raw: string): string {
  if (/test_crawl quality check failed/i.test(raw))
    return "자동 생성된 크롤러가 실제 내용을 가져오지 못했습니다. 사이트가 로그인이 필요하거나 매우 복잡한 구조일 수 있습니다.";
  if (/codex exceeded|timeout/i.test(raw))
    return "AI가 주어진 시간 안에 크롤러를 완성하지 못했습니다. 재시도하거나 타임아웃을 늘려주세요.";
  if (/OPENAI_API_KEY not set/i.test(raw))
    return "OpenAI API 키가 설정되지 않았습니다.";
  return "";
}

function classifyLine(line: string): { nextPhase?: Phase } {
  // Try JSON parse first
  try {
    const ev = JSON.parse(line) as { type?: string; message?: string; success?: boolean };
    if (ev.type === "result") {
      return { nextPhase: ev.success ? "done" : "failed" };
    }
    const msg = ev.message ?? "";
    if (/apply_patch|write to crawler\/sites\/custom|```python/i.test(msg)) {
      return { nextPhase: "building" };
    }
    if (/python\s+-[<c]|verification script|test_crawl/i.test(msg)) {
      return { nextPhase: "verifying" };
    }
    if (/saved:\s*\d+|quality check/i.test(msg)) {
      return { nextPhase: "saving" };
    }
    if (/web_search|fetch_page|curl|Traceback|AssertionError/i.test(msg)) {
      // analyzing -> still analyzing unless we're past that
    }
    return {};
  } catch {
    // raw stdout line
  }
  if (/apply_patch|write to crawler\/sites\/custom/i.test(line)) {
    return { nextPhase: "building" };
  }
  if (/python\s+-[<c]|test_crawl/i.test(line)) {
    return { nextPhase: "verifying" };
  }
  if (/saved:\s*\d+/i.test(line)) {
    return { nextPhase: "saving" };
  }
  return {};
}

function formatElapsed(ms: number): string {
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}초`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return rem > 0 ? `${m}분 ${rem}초` : `${m}분`;
}

export default function AutoAddPage() {
  const [url, setUrl] = useState("");
  const [siteId, setSiteId] = useState("");

  const [logFile, setLogFile] = useState<string | null>(null);
  const [logStreamKey, setLogStreamKey] = useState(0);
  const [logLines, setLogLines] = useState<string[]>([]);
  const [result, setResult] = useState<ResultEvent | null>(null);

  // Phase state
  const [phase, setPhase] = useState<Phase>("analyzing");
  const [phaseStartTimes, setPhaseStartTimes] = useState<Partial<Record<Phase, number>>>({});
  const [phaseDurations, setPhaseDurations] = useState<Partial<Record<Phase, number>>>({});
  const [now, setNow] = useState(0);
  const [lastLineAt, setLastLineAt] = useState<number | null>(null);
  const [isRunning, setIsRunning] = useState(false);
  const [idleWarning, setIdleWarning] = useState(false);

  const logEsRef = useRef<EventSource | null>(null);
  const logPaneRef = useRef<HTMLDivElement>(null);
  const phaseRef = useRef<Phase>("analyzing");

  const inputClass =
    "px-3 py-2 border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 text-slate-900 dark:text-slate-100 rounded-lg text-sm";

  // Elapsed-time ticker
  useEffect(() => {
    if (!isRunning) return;
    const iv = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(iv);
  }, [isRunning]);

  // Idle detection
  useEffect(() => {
    if (!isRunning || !lastLineAt) return;
    const iv = setInterval(() => {
      setIdleWarning(Date.now() - lastLineAt > 90_000);
    }, 5000);
    return () => clearInterval(iv);
  }, [isRunning, lastLineAt]);

  const advancePhase = (nextPhase: Phase) => {
    const current = phaseRef.current;
    if (phaseIndex(nextPhase) <= phaseIndex(current)) return;
    const ts = Date.now();
    // Record duration for current phase if it was in the 4 main phases
    setPhaseDurations((prev) => ({
      ...prev,
      [current]: ts - (phaseStartTimes[current] ?? ts),
    }));
    phaseRef.current = nextPhase;
    setPhase(nextPhase);
    setPhaseStartTimes((prev) => ({ ...prev, [nextPhase]: ts }));
  };

  // Open log stream
  const openLog = (name: string, jobStartedAt?: number) => {
    logEsRef.current?.close();
    logEsRef.current = null;
    setLogLines([]);
    setResult(null);
    setLogFile(name);
    setLogStreamKey((prev) => prev + 1);
    setPhase("analyzing");
    phaseRef.current = "analyzing";
    const startedAt = jobStartedAt ?? Date.now();
    setNow(startedAt);
    setPhaseStartTimes({ analyzing: startedAt });
    setPhaseDurations({});
    setLastLineAt(null);
    setIdleWarning(false);
    setIsRunning(true);
  };

  useEffect(() => {
    if (!logFile) return;
    const es = new EventSource(
      `/api/crawler/logs?file=${encodeURIComponent(logFile)}&stream=${logStreamKey}`
    );
    logEsRef.current = es;
    es.onmessage = (e) => {
      const line: string = e.data;
      setLogLines((prev) => {
        const next = [...prev, line];
        return next.length > 500 ? next.slice(-500) : next;
      });
      setLastLineAt(Date.now());

      // Classify line for phase transitions
      const { nextPhase } = classifyLine(line);
      if (nextPhase) advancePhase(nextPhase);

      // Parse result event
      try {
        const ev = JSON.parse(line) as { type?: string; [k: string]: unknown };
        if (ev.type === "result") {
          const res = ev as unknown as ResultEvent;
          setResult(res);
          setIsRunning(false);
          setIdleWarning(false);
        }
      } catch {
        // raw line
      }
    };
    es.onerror = () => {
      es.close();
    };
    return () => {
      es.close();
      logEsRef.current = null;
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [logFile, logStreamKey]);

  useEffect(() => {
    if (logPaneRef.current) {
      logPaneRef.current.scrollTop = logPaneRef.current.scrollHeight;
    }
  }, [logLines]);

  // Auto-attach to most recent live job on page load
  useEffect(() => {
    (async () => {
      const res = await fetch("/api/auto-add/codex", { cache: "no-store" });
      const data = await res.json();
      const jobs: CodexJob[] = data.jobs ?? [];
      if (jobs.length > 0) {
        const latest = jobs[jobs.length - 1];
        const logName = latest.logPath.split("/").pop();
        if (logName) {
          openLog(logName, new Date(latest.startedAt).getTime());
        }
      }
    })();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const start = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!url.trim()) return;
    const res = await fetch("/api/auto-add/codex", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        action: "start",
        url,
        siteId: siteId.trim() || undefined,
        siteName: undefined,
        timeoutSeconds: 1200,
      }),
    });
    const data = await res.json();
    if (data.error) {
      alert(`오류: ${data.error}`);
      return;
    }
    const logName = data.job?.logPath?.split("/").pop() as string | undefined;
    if (logName) openLog(logName, Date.now());
  };

  const renderLogLine = (line: string, i: number) => {
    let cls = "text-emerald-300";
    try {
      const ev = JSON.parse(line) as { type?: string };
      if (ev.type === "result") cls = "text-amber-300 font-semibold";
      else if (ev.type === "error") cls = "text-red-400";
    } catch {
      // raw line
    }
    return (
      <div key={i} className={`whitespace-pre-wrap break-all ${cls}`}>
        {line}
      </div>
    );
  };

  // Phase card rendering helpers
  const getPhaseStatus = (p: PhaseInfo): "pending" | "in-progress" | "done" | "failed" => {
    if (phase === "done" || phaseIndex(p.key as Phase) < phaseIndex(phase)) {
      return "done";
    }
    if (phase === "failed" && phaseIndex(p.key as Phase) === phaseIndex("saving")) {
      return "failed";
    }
    if (p.key === phase) return "in-progress";
    return "pending";
  };

  const getElapsedForPhase = (p: PhaseInfo): string | null => {
    const status = getPhaseStatus(p);
    if (status === "done") {
      const dur = phaseDurations[p.key];
      if (dur != null) return `(${formatElapsed(dur)})`;
      return null;
    }
    if (status === "in-progress") {
      const start = phaseStartTimes[p.key];
      if (start) return `(${formatElapsed(now - start)} 경과)`;
      return null;
    }
    return null;
  };

  const statusIcon = (status: "pending" | "in-progress" | "done" | "failed") => {
    if (status === "done") return <span className="text-emerald-500 font-bold">✓</span>;
    if (status === "in-progress") return <span className="text-amber-500">⏳</span>;
    if (status === "failed") return <span className="text-red-500 font-bold">✗</span>;
    return <span className="text-slate-400">·</span>;
  };

  const showPhaseCard = isRunning || phase === "done" || phase === "failed";

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Auto-Add</h1>
        <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
          URL 입력만으로 Codex가 자율적으로 크롤러를 분석하고 Python 코드를
          작성합니다. 10~15분 정도 걸립니다.
        </p>
      </div>

      {/* Start form */}
      <form
        onSubmit={start}
        className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 p-5 space-y-4"
      >
        <h2 className="text-sm font-semibold text-slate-700 dark:text-slate-300">
          새 Auto-Add 실행
        </h2>
        <div>
          <label className="text-xs font-semibold text-slate-600 dark:text-slate-300 mb-1 block">
            URL <span className="text-red-500">*</span>
          </label>
          <input
            type="url"
            required
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://example.com/board/list"
            disabled={isRunning}
            className={`w-full ${inputClass} py-2.5 focus:outline-none focus:ring-2 focus:ring-amber-500 focus:border-transparent font-mono disabled:opacity-60 disabled:cursor-not-allowed`}
          />
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div>
            <label className="text-xs font-semibold text-slate-600 dark:text-slate-300 mb-1 block">
              site_id{" "}
              <span className="font-normal text-slate-400">(선택)</span>
            </label>
            <input
              type="text"
              value={siteId}
              onChange={(e) => setSiteId(e.target.value)}
              placeholder="my-site-id (비워두면 자동 생성)"
              disabled={isRunning}
              className={`w-full ${inputClass} disabled:opacity-60 disabled:cursor-not-allowed`}
            />
          </div>
          <div className="flex items-end">
            <button
              type="submit"
              disabled={!url.trim() || isRunning}
              className="w-full px-5 py-2 bg-amber-600 hover:bg-amber-700 text-white rounded-lg text-sm font-medium disabled:bg-slate-300 dark:disabled:bg-slate-700 disabled:cursor-not-allowed flex items-center justify-center gap-2 transition-colors"
            >
              {isRunning ? (
                <>
                  <span className="inline-block w-3.5 h-3.5 border-2 border-white/70 border-t-transparent rounded-full animate-spin" aria-hidden="true" />
                  동작중...
                </>
              ) : (
                "시작"
              )}
            </button>
          </div>
        </div>
      </form>

      {/* Phase card */}
      {showPhaseCard && !result && (
        <section className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 p-5 space-y-4">
          <h2 className="text-sm font-semibold text-slate-700 dark:text-slate-300">
            사이트 자동 분석 및 크롤러 제작 중
          </h2>
          <ul className="space-y-3">
            {PHASES.map((p, i) => {
              const status = getPhaseStatus(p);
              const elapsed = getElapsedForPhase(p);
              return (
                <li key={p.key} className="flex items-center gap-3 text-sm">
                  <span className="w-5 text-center text-base leading-none">
                    {statusIcon(status)}
                  </span>
                  <span className="text-slate-400 dark:text-slate-500 text-xs w-5 shrink-0">
                    {i + 1}단계:
                  </span>
                  <span
                    className={
                      status === "in-progress"
                        ? "font-medium text-slate-900 dark:text-slate-100"
                        : status === "done"
                        ? "text-slate-500 dark:text-slate-400 line-through"
                        : "text-slate-400 dark:text-slate-500"
                    }
                  >
                    {p.label}
                    {status === "in-progress" && " 중..."}
                  </span>
                  {elapsed && (
                    <span className="text-xs text-slate-400 dark:text-slate-500 ml-1">
                      {elapsed}
                    </span>
                  )}
                  {status === "pending" && (
                    <span className="text-xs text-slate-400 dark:text-slate-500">
                      대기 중
                    </span>
                  )}
                </li>
              );
            })}
          </ul>

          {/* Idle warning */}
          {idleWarning && (
            <div className="rounded-lg bg-amber-50 dark:bg-amber-950/40 border border-amber-200 dark:border-amber-800 px-4 py-3 text-sm text-amber-800 dark:text-amber-200">
              ⚠ 응답이 없습니다. 아직 작업 중이거나 연결이 끊어졌을 수 있습니다.
            </div>
          )}

          {/* Raw log — collapsed */}
          {logFile && (
            <details className="mt-2">
              <summary className="cursor-pointer text-xs text-slate-500 dark:text-slate-400 select-none hover:text-slate-700 dark:hover:text-slate-200">
                개발자용 원본 로그 ({logLines.length}줄)
              </summary>
              <div className="mt-2 bg-slate-950 dark:bg-black rounded-lg border border-slate-800 overflow-hidden flex flex-col">
                <div className="bg-slate-900 px-4 py-2 text-xs font-mono text-slate-300 border-b border-slate-800">
                  {logFile}
                </div>
                <div
                  ref={logPaneRef}
                  className="flex-1 overflow-y-auto p-4 font-mono text-xs max-h-[350px] min-h-[120px]"
                >
                  {logLines.length === 0 ? (
                    <div className="text-slate-500">스트림 연결 중...</div>
                  ) : (
                    logLines.map((line, i) => renderLogLine(line, i))
                  )}
                </div>
              </div>
            </details>
          )}
        </section>
      )}

      {/* Success card */}
      {result?.success && (
        <section className="rounded-xl border border-emerald-200 dark:border-emerald-900 bg-emerald-50 dark:bg-emerald-950/40 p-5 space-y-3">
          <div className="flex items-center gap-2 text-emerald-800 dark:text-emerald-200 font-semibold text-lg">
            ✅ 수집 준비 완료
          </div>
          <p className="text-sm text-emerald-700 dark:text-emerald-300">
            전용 크롤러가 제작되어 검증에서{" "}
            <strong>{result.count ?? 0}건</strong>을 수집했습니다.
          </p>
          <dl className="space-y-1 text-sm">
            <div className="flex gap-2">
              <dt className="text-emerald-600 dark:text-emerald-400 font-medium min-w-[80px]">크롤러 ID</dt>
              <dd className="font-mono text-emerald-900 dark:text-emerald-100">{result.site_id ?? "-"}</dd>
            </div>
            {result.first_title && (
              <div className="flex gap-2">
                <dt className="text-emerald-600 dark:text-emerald-400 font-medium min-w-[80px]">예시 제목</dt>
                <dd className="text-emerald-900 dark:text-emerald-100 truncate max-w-xs">
                  {result.first_title.slice(0, 60)}
                </dd>
              </div>
            )}
          </dl>
          {result.site_id && (
            <div className="pt-1">
              <Link
                href={`/crawler?site=${result.site_id}&from=auto-add`}
                className="inline-flex items-center gap-2 px-4 py-2 bg-emerald-600 hover:bg-emerald-700 text-white rounded-lg text-sm font-medium transition-colors"
              >
                실제 수집 시작 →
              </Link>
            </div>
          )}
          {/* Collapsed raw log */}
          {logFile && (
            <details className="mt-1">
              <summary className="cursor-pointer text-xs text-slate-500 dark:text-slate-400 select-none hover:text-slate-700 dark:hover:text-slate-200">
                개발자용 원본 로그 ({logLines.length}줄)
              </summary>
              <div className="mt-2 bg-slate-950 rounded-lg border border-slate-800 overflow-hidden">
                <div
                  ref={logPaneRef}
                  className="overflow-y-auto p-4 font-mono text-xs max-h-[300px]"
                >
                  {logLines.map((line, i) => renderLogLine(line, i))}
                </div>
              </div>
            </details>
          )}
        </section>
      )}

      {/* Failure card */}
      {result && !result.success && (
        <section className="rounded-xl border border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-950/40 p-5 space-y-3">
          <div className="flex items-center gap-2 text-red-800 dark:text-red-200 font-semibold text-lg">
            ❌ 수집 실패
          </div>
          {result.elapsed_seconds != null && (
            <p className="text-sm text-red-600 dark:text-red-400">
              총 경과 시간: {formatElapsed(result.elapsed_seconds * 1000)}
            </p>
          )}
          {(() => {
            const raw = result.reason ?? result.error ?? "알 수 없는 오류";
            const translated = translateError(raw);
            return (
              <div className="space-y-2">
                {translated ? (
                  <p className="text-sm text-red-700 dark:text-red-300">{translated}</p>
                ) : (
                  <pre className="text-xs font-mono bg-red-100 dark:bg-red-950 text-red-800 dark:text-red-200 rounded p-3 whitespace-pre-wrap break-all">
                    {raw}
                  </pre>
                )}
              </div>
            );
          })()}
          <div className="flex gap-3 pt-1">
            <button
              onClick={() => {
                setResult(null);
                setPhase("analyzing");
                phaseRef.current = "analyzing";
                setPhaseStartTimes({});
                setPhaseDurations({});
                setLogFile(null);
                setLogLines([]);
                setIsRunning(false);
              }}
              className="px-4 py-2 bg-red-600 hover:bg-red-700 text-white rounded-lg text-sm font-medium"
            >
              🔄 재시도
            </button>
            <details className="inline">
              <summary className="cursor-pointer px-4 py-2 border border-red-300 dark:border-red-700 text-red-700 dark:text-red-300 rounded-lg text-sm font-medium hover:bg-red-100 dark:hover:bg-red-900/40 select-none">
                📋 원본 로그 보기
              </summary>
              <div className="mt-2 bg-slate-950 rounded-lg border border-slate-800 overflow-hidden">
                <div className="overflow-y-auto p-4 font-mono text-xs max-h-[300px]">
                  {logLines.map((line, i) => renderLogLine(line, i))}
                </div>
              </div>
            </details>
          </div>
        </section>
      )}
    </div>
  );
}
