"use client";

import { useCallback, useEffect, useState } from "react";

type LastRun = {
  status: string; new_count: number | null; finished_at: string | null;
} | null;
type CorpusCard = {
  key: string; label: string; total: number | null;
  last_collected: string | null; db_exists: boolean;
  last_run: LastRun; busy: boolean;
};
type Run = {
  id: number; corpus: string; started_at: string; finished_at: string | null;
  status: string; new_count: number | null; total_after: number | null;
};

function freshness(last: string | null): { dot: string; text: string } {
  if (!last) return { dot: "bg-gray-400", text: "수집 이력 없음" };
  const days = (Date.now() - new Date(last.replace(" ", "T")).getTime()) / 86400000;
  if (days < 7) return { dot: "bg-green-500", text: "최신" };
  if (days < 30) return { dot: "bg-yellow-500", text: `${Math.floor(days)}일 경과` };
  return { dot: "bg-red-500", text: `${Math.floor(days)}일 경과` };
}

export default function Home() {
  const [corpora, setCorpora] = useState<CorpusCard[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [logRunId, setLogRunId] = useState<number | null>(null);
  const [logText, setLogText] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const [c, r] = await Promise.all([
        fetch("/api/corpora").then((x) => x.json()),
        fetch("/api/runs?limit=30").then((x) => x.json()),
      ]);
      setCorpora(c);
      setRuns(r);
      setError("");
    } catch {
      setError("API 서버(:8500)에 연결할 수 없습니다");
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [load]);

  useEffect(() => {
    if (logRunId === null) return;
    const fetchLog = () =>
      fetch(`/api/runs/${logRunId}/log?tail=200`)
        .then((x) => (x.ok ? x.text() : "(로그 없음)"))
        .then(setLogText)
        .catch(() => setLogText("(로그를 불러올 수 없습니다)"));
    fetchLog();
    const t = setInterval(fetchLog, 3000);
    return () => clearInterval(t);
  }, [logRunId]);

  const refresh = async (key: string) => {
    try {
      const r = await fetch(`/api/corpora/${key}/refresh`, { method: "POST" });
      if (r.status === 409) setError("다른 수집이 실행 중입니다");
    } catch {
      setError("Refresh 요청 실패 — API 서버(:8500)에 연결할 수 없습니다");
    }
    load();
  };

  const anyBusy = corpora.some((c) => c.busy);

  return (
    <main className="mx-auto max-w-6xl p-8 font-sans">
      <h1 className="mb-1 text-2xl font-bold">FINO Ops — 코퍼스 최신화</h1>
      <p className="mb-6 text-sm text-gray-500">
        {anyBusy ? "수집 실행 중…" : "대기 중"} · 5초마다 갱신
      </p>
      {error && <p className="mb-4 rounded bg-red-50 p-2 text-sm text-red-700">{error}</p>}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {corpora.map((c) => {
          const f = freshness(c.last_collected);
          return (
            <div key={c.key} className="rounded-lg border p-4 shadow-sm">
              <div className="flex items-center justify-between">
                <span className="font-semibold">{c.label}</span>
                <span className={`h-3 w-3 rounded-full ${f.dot}`} title={f.text} />
              </div>
              <p className="mt-2 text-2xl font-bold">
                {c.total?.toLocaleString() ?? "—"}
              </p>
              <p className="text-xs text-gray-500">
                마지막 수집 {c.last_collected ?? "—"} · {f.text}
              </p>
              <p className="text-xs text-gray-500">
                최근 실행:{" "}
                {c.last_run
                  ? `${c.last_run.status} (+${c.last_run.new_count ?? 0})`
                  : "—"}
              </p>
              <button
                onClick={() => refresh(c.key)}
                disabled={anyBusy || !c.db_exists}
                className="mt-3 w-full rounded bg-blue-600 px-3 py-1.5 text-sm text-white disabled:bg-gray-300"
              >
                {anyBusy ? "실행 중…" : "Refresh"}
              </button>
            </div>
          );
        })}
      </div>

      <h2 className="mt-10 mb-3 text-lg font-semibold">실행 이력</h2>
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b text-left text-gray-500">
            <th className="py-1">#</th><th>코퍼스</th><th>시작</th>
            <th>상태</th><th>신규</th><th>총계</th><th>로그</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.id} className="border-b">
              <td className="py-1">{r.id}</td>
              <td>{r.corpus}</td>
              <td>{r.started_at}</td>
              <td className={r.status === "ok" ? "text-green-600"
                : r.status === "running" ? "text-blue-600" : "text-red-600"}>
                {r.status}
              </td>
              <td>{r.new_count ?? "—"}</td>
              <td>{r.total_after?.toLocaleString() ?? "—"}</td>
              <td>
                <button className="text-blue-600 underline" onClick={() => setLogRunId(r.id)}>
                  보기
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {logRunId !== null && (
        <div className="fixed inset-0 flex items-center justify-center bg-black/40 p-8"
             onClick={() => setLogRunId(null)}>
          <div className="max-h-[80vh] w-full max-w-3xl overflow-auto rounded bg-gray-900 p-4"
               onClick={(e) => e.stopPropagation()}>
            <div className="mb-2 flex justify-between text-sm text-gray-300">
              <span>run #{logRunId} 로그 (3초 갱신)</span>
              <button onClick={() => setLogRunId(null)}>닫기 ✕</button>
            </div>
            <pre className="whitespace-pre-wrap text-xs text-green-300">{logText}</pre>
          </div>
        </div>
      )}
    </main>
  );
}
