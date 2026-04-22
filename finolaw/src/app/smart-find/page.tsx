"use client";

import { useRef, useState } from "react";

interface FoundDocument {
  id: string;
  title: string;
  file_url: string;
  file_type: string;
  source_page: string;
  date: string | null;
}

interface RunResult {
  url: string;
  documents: FoundDocument[];
  pages_scanned: number;
  detail_pages_visited: number;
  fetch_method: string;
  errors: string[];
}

export default function SmartFindPage() {
  const [url, setUrl] = useState("");
  const [maxPages, setMaxPages] = useState(20);
  const [maxDepth, setMaxDepth] = useState(3);
  const [provider, setProvider] = useState<"gpt" | "gemini">("gpt");
  const [useAi, setUseAi] = useState(true);
  const [saveDb, setSaveDb] = useState(false);

  const [isRunning, setIsRunning] = useState(false);
  const [progress, setProgress] = useState<string[]>([]);
  const [result, setResult] = useState<RunResult | null>(null);
  const [dbSaved, setDbSaved] = useState<{
    count: number;
    siteId: string;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const abortRef = useRef<AbortController | null>(null);

  const inputClass =
    "px-3 py-2 border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 text-slate-900 dark:text-slate-100 rounded-lg text-sm";

  const start = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!url.trim()) return;
    setIsRunning(true);
    setProgress([]);
    setResult(null);
    setDbSaved(null);
    setError(null);

    const ac = new AbortController();
    abortRef.current = ac;

    try {
      const res = await fetch("/api/smart-find", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          url,
          maxPages,
          maxDepth,
          provider,
          noAi: !useAi,
          saveDb,
        }),
        signal: ac.signal,
      });
      if (!res.ok || !res.body) {
        const txt = await res.text().catch(() => "");
        throw new Error(`HTTP ${res.status}: ${txt}`);
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop() ?? "";
        for (const part of parts) {
          const lines = part.split("\n");
          for (const line of lines) {
            if (!line.startsWith("data: ")) continue;
            const payload = line.slice(6).trim();
            if (!payload) continue;
            try {
              const ev = JSON.parse(payload) as {
                type: string;
                [k: string]: unknown;
              };
              if (ev.type === "progress") {
                setProgress((p) => [...p, ev.message as string]);
              } else if (ev.type === "result") {
                setResult(ev as unknown as RunResult);
              } else if (ev.type === "db_saved") {
                setDbSaved({
                  count: ev.count as number,
                  siteId: ev.site_id as string,
                });
              } else if (ev.type === "error") {
                setError((ev.message as string) ?? "unknown error");
              }
            } catch {
              // ignore parse errors
            }
          }
        }
      }
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setIsRunning(false);
      abortRef.current = null;
    }
  };

  const cancel = () => {
    abortRef.current?.abort();
    setIsRunning(false);
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">URL 크롤링 (Smart Find)</h1>
        <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
          URL 하나만 입력하면 자동으로 다운로드 가능한 문서를 찾아냅니다.
          패턴 매칭이 실패하면 LLM(GPT-5.4-mini / Gemini-2.5-flash)으로 구조
          분석을 시도합니다.
        </p>
      </div>

      <form
        onSubmit={start}
        className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 p-5 space-y-4"
      >
        <div>
          <label className="text-xs font-semibold text-slate-600 dark:text-slate-300 mb-1 block">
            URL
          </label>
          <input
            type="url"
            required
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://example.com/board/list"
            className={`w-full ${inputClass} py-2.5 focus:outline-none focus:ring-2 focus:ring-purple-500 focus:border-transparent font-mono`}
            disabled={isRunning}
          />
        </div>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <div>
            <label className="text-xs font-semibold text-slate-600 dark:text-slate-300 mb-1 block">
              최대 페이지
            </label>
            <input
              type="number"
              min={1}
              max={200}
              value={maxPages}
              onChange={(e) => setMaxPages(parseInt(e.target.value, 10) || 1)}
              className={`w-full ${inputClass}`}
              disabled={isRunning}
            />
          </div>
          <div>
            <label className="text-xs font-semibold text-slate-600 dark:text-slate-300 mb-1 block">
              탐색 깊이
            </label>
            <input
              type="number"
              min={0}
              max={5}
              value={maxDepth}
              onChange={(e) => setMaxDepth(parseInt(e.target.value, 10) || 0)}
              className={`w-full ${inputClass}`}
              disabled={isRunning}
            />
          </div>
          <div>
            <label className="text-xs font-semibold text-slate-600 dark:text-slate-300 mb-1 block">
              LLM Provider
            </label>
            <select
              value={provider}
              onChange={(e) =>
                setProvider(e.target.value as "gpt" | "gemini")
              }
              className={`w-full ${inputClass}`}
              disabled={isRunning}
            >
              <option value="gpt">GPT-5.4-mini</option>
              <option value="gemini">Gemini-2.5-flash</option>
            </select>
          </div>
          <div className="flex items-end gap-4 pb-1">
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={useAi}
                onChange={(e) => setUseAi(e.target.checked)}
                disabled={isRunning}
              />
              AI 분석 사용
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={saveDb}
                onChange={(e) => setSaveDb(e.target.checked)}
                disabled={isRunning}
              />
              DB에 저장
            </label>
          </div>
        </div>

        <div className="flex items-center justify-between pt-2">
          <div className="text-xs text-slate-500 dark:text-slate-400">
            {isRunning ? "실행 중..." : result ? "완료" : "준비"}
          </div>
          <div className="flex gap-2">
            {isRunning ? (
              <button
                type="button"
                onClick={cancel}
                className="px-5 py-2 bg-red-600 hover:bg-red-700 text-white rounded-lg text-sm font-medium"
              >
                중지
              </button>
            ) : (
              <button
                type="submit"
                disabled={!url.trim()}
                className="px-5 py-2 bg-purple-600 hover:bg-purple-700 text-white rounded-lg text-sm font-medium disabled:bg-slate-300 dark:disabled:bg-slate-700 disabled:cursor-not-allowed"
              >
                시작
              </button>
            )}
          </div>
        </div>
      </form>

      {error && (
        <div className="rounded-xl border border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-950/40 p-4 text-sm text-red-700 dark:text-red-300">
          <span className="font-semibold">오류:</span> {error}
        </div>
      )}

      {(isRunning || progress.length > 0) && (
        <section className="bg-slate-950 dark:bg-black rounded-xl border border-slate-800 p-4">
          <h2 className="text-xs font-semibold text-slate-400 mb-2 font-mono">
            PROGRESS
          </h2>
          <div className="max-h-60 overflow-y-auto font-mono text-xs text-emerald-300 space-y-0.5">
            {progress.map((line, i) => (
              <div key={i} className="opacity-90">
                <span className="text-slate-500">[{i + 1}]</span> {line}
              </div>
            ))}
            {isRunning && (
              <div className="text-amber-400 animate-pulse">▋ 처리 중...</div>
            )}
          </div>
        </section>
      )}

      {result && (
        <section className="space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-semibold">
              결과 — {result.documents.length}개 파일 발견
            </h2>
            <div className="text-xs text-slate-500 dark:text-slate-400 flex items-center gap-3">
              <span>페이지 {result.pages_scanned}</span>
              <span>상세 {result.detail_pages_visited}</span>
              <span className="px-2 py-0.5 bg-slate-200 dark:bg-slate-700 text-slate-700 dark:text-slate-200 rounded font-mono">
                {result.fetch_method}
              </span>
            </div>
          </div>

          {dbSaved && (
            <div className="rounded-lg border border-emerald-200 dark:border-emerald-900 bg-emerald-50 dark:bg-emerald-950/40 px-4 py-2.5 text-sm text-emerald-800 dark:text-emerald-200">
              ✓ DB에 {dbSaved.count}건 저장 ·{" "}
              <code className="font-mono">{dbSaved.siteId}</code>
            </div>
          )}

          {result.errors.length > 0 && (
            <details className="rounded-lg border border-amber-200 dark:border-amber-900 bg-amber-50 dark:bg-amber-950/40 px-4 py-2.5 text-sm text-amber-800 dark:text-amber-200">
              <summary className="cursor-pointer font-medium">
                경고 {result.errors.length}건
              </summary>
              <ul className="mt-2 list-disc pl-5 text-xs space-y-0.5">
                {result.errors.map((e, i) => (
                  <li key={i}>{e}</li>
                ))}
              </ul>
            </details>
          )}

          <div className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 overflow-hidden">
            {result.documents.length === 0 ? (
              <div className="px-6 py-12 text-center text-slate-400 dark:text-slate-500 text-sm">
                다운로드 가능한 문서를 찾지 못했습니다.
              </div>
            ) : (
              <ul className="divide-y divide-slate-100 dark:divide-slate-800">
                {result.documents.map((d) => (
                  <li
                    key={d.id}
                    className="px-5 py-3 hover:bg-slate-50 dark:hover:bg-slate-800/50"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span
                            className={`px-2 py-0.5 rounded text-xs font-mono font-medium uppercase ${
                              fileTypeColor(d.file_type)
                            }`}
                          >
                            {d.file_type}
                          </span>
                          <span className="font-medium text-slate-800 dark:text-slate-100 truncate">
                            {d.title || "(제목 없음)"}
                          </span>
                        </div>
                        <div className="text-xs text-slate-500 dark:text-slate-400 mt-1 truncate font-mono">
                          {d.file_url}
                        </div>
                      </div>
                      <a
                        href={d.file_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="shrink-0 text-slate-400 dark:text-slate-500 hover:text-blue-600 dark:hover:text-blue-400 text-lg"
                      >
                        ↗
                      </a>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </section>
      )}
    </div>
  );
}

function fileTypeColor(type: string): string {
  const colors: Record<string, string> = {
    pdf: "bg-red-100 dark:bg-red-950/40 text-red-700 dark:text-red-300",
    hwp: "bg-blue-100 dark:bg-blue-950/40 text-blue-700 dark:text-blue-300",
    hwpx: "bg-blue-100 dark:bg-blue-950/40 text-blue-700 dark:text-blue-300",
    xlsx: "bg-green-100 dark:bg-green-950/40 text-green-700 dark:text-green-300",
    xls: "bg-green-100 dark:bg-green-950/40 text-green-700 dark:text-green-300",
    csv: "bg-green-100 dark:bg-green-950/40 text-green-700 dark:text-green-300",
    docx: "bg-indigo-100 dark:bg-indigo-950/40 text-indigo-700 dark:text-indigo-300",
    doc: "bg-indigo-100 dark:bg-indigo-950/40 text-indigo-700 dark:text-indigo-300",
    pptx: "bg-orange-100 dark:bg-orange-950/40 text-orange-700 dark:text-orange-300",
    zip: "bg-amber-100 dark:bg-amber-950/40 text-amber-700 dark:text-amber-300",
  };
  return (
    colors[type.toLowerCase()] ||
    "bg-slate-100 dark:bg-slate-800 text-slate-500 dark:text-slate-400"
  );
}
