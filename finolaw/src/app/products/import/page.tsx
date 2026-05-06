"use client";

import { useEffect, useMemo, useState } from "react";

interface ImportFileResult {
  fileName: string;
  rowsRead: number;
  inserted: number;
  updated: number;
  skipped: number;
  errors: string[];
}

interface ProductRow {
  id: string;
  manufacturer_part_number: string | null;
  mouser_part_number: string | null;
  manufacturer: string | null;
  description: string | null;
  availability: string | null;
  price: string | null;
  product_url: string | null;
  datasheet_url: string | null;
  updated_at: string;
}

interface ProductsSummary {
  total: number;
  manufacturers: number;
  lastUpdated: string | null;
  recent: ProductRow[];
}

interface ImportResponse {
  totals: {
    rowsRead: number;
    inserted: number;
    updated: number;
    skipped: number;
    errors: number;
  };
  files: ImportFileResult[];
  summary: ProductsSummary;
}

function formatDate(value: string | null): string {
  if (!value) return "-";
  try {
    return new Date(value).toLocaleString("ko-KR", {
      dateStyle: "medium",
      timeStyle: "short",
    });
  } catch {
    return value;
  }
}

function formatNum(value: number): string {
  return value.toLocaleString("ko-KR");
}

export default function ProductImportPage() {
  const [files, setFiles] = useState<File[]>([]);
  const [source, setSource] = useState("mouser");
  const [sourceQuery, setSourceQuery] = useState("bjt");
  const [downloadUrls, setDownloadUrls] = useState("");
  const [cookieHeader, setCookieHeader] = useState("");
  const [summary, setSummary] = useState<ProductsSummary | null>(null);
  const [result, setResult] = useState<ImportResponse | null>(null);
  const [downloadLoading, setDownloadLoading] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const totalSize = useMemo(
    () => files.reduce((sum, file) => sum + file.size, 0),
    [files]
  );

  useEffect(() => {
    fetch("/api/products/import", { cache: "no-store" })
      .then((res) => res.json())
      .then((data) => setSummary(data))
      .catch(() => {});
  }, []);

  const importFiles = async () => {
    if (files.length === 0 || loading) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const form = new FormData();
      form.set("source", source);
      form.set("sourceQuery", sourceQuery);
      files.forEach((file) => form.append("files", file));
      const res = await fetch("/api/products/import", {
        method: "POST",
        body: form,
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        setError(data.error ?? res.statusText);
        return;
      }
      setResult(data);
      setSummary(data.summary);
      setFiles([]);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  const importDownloadUrls = async () => {
    const urls = downloadUrls
      .split(/\s+/)
      .map((value) => value.trim())
      .filter(Boolean);
    if (urls.length === 0 || !cookieHeader.trim() || downloadLoading) return;
    setDownloadLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await fetch("/api/products/import", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          source,
          sourceQuery,
          urls,
          cookie: cookieHeader,
        }),
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        setError(data.error ?? res.statusText);
        return;
      }
      setResult(data);
      setSummary(data.summary);
      setCookieHeader("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setDownloadLoading(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <h1 className="text-2xl font-bold">제품 CSV Import</h1>
          <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
            Mouser 페이지별 CSV를 여러 개 선택하면 하나로 합쳐 중복 없이 저장합니다.
          </p>
        </div>
        <div className="flex gap-3">
          <Metric label="제품" value={summary ? formatNum(summary.total) : "-"} />
          <Metric
            label="제조사"
            value={summary ? formatNum(summary.manufacturers) : "-"}
          />
        </div>
      </div>

      <section className="rounded-xl border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900">
        <div className="grid gap-4 md:grid-cols-[1fr_180px_180px]">
          <label className="block">
            <span className="mb-1 block text-xs font-semibold text-slate-600 dark:text-slate-300">
              CSV 파일
            </span>
            <input
              type="file"
              accept=".csv,text/csv"
              multiple
              onChange={(event) => setFiles(Array.from(event.target.files ?? []))}
              className="block w-full rounded-lg border border-dashed border-slate-300 bg-slate-50 px-3 py-3 text-sm text-slate-700 file:mr-3 file:rounded-md file:border-0 file:bg-slate-900 file:px-3 file:py-1.5 file:text-xs file:font-medium file:text-white dark:border-slate-700 dark:bg-slate-950 dark:text-slate-200 dark:file:bg-slate-700"
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs font-semibold text-slate-600 dark:text-slate-300">
              source
            </span>
            <input
              value={source}
              onChange={(event) => setSource(event.target.value)}
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-950"
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs font-semibold text-slate-600 dark:text-slate-300">
              검색어/묶음
            </span>
            <input
              value={sourceQuery}
              onChange={(event) => setSourceQuery(event.target.value)}
              placeholder="bjt"
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-950"
            />
          </label>
        </div>

        <div className="mt-4 flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div className="text-xs text-slate-500 dark:text-slate-400">
            선택됨: {files.length}개
            {files.length > 0 && (
              <span> · {(totalSize / 1024).toFixed(1)} KB</span>
            )}
          </div>
          <button
            onClick={importFiles}
            disabled={files.length === 0 || loading}
            className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-300 dark:bg-slate-700 dark:hover:bg-slate-600 dark:disabled:bg-slate-800 dark:disabled:text-slate-500"
          >
            {loading ? "Import 중..." : "CSV 다중 Import"}
          </button>
        </div>

        {files.length > 0 && (
          <ul className="mt-4 divide-y divide-slate-100 rounded-lg border border-slate-200 text-sm dark:divide-slate-800 dark:border-slate-800">
            {files.map((file) => (
              <li
                key={`${file.name}-${file.size}-${file.lastModified}`}
                className="flex items-center justify-between gap-3 px-3 py-2"
              >
                <span className="truncate font-mono text-xs">{file.name}</span>
                <span className="shrink-0 text-xs text-slate-400">
                  {(file.size / 1024).toFixed(1)} KB
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="rounded-xl border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900">
        <div className="flex flex-col gap-1">
          <h2 className="text-sm font-semibold text-slate-700 dark:text-slate-300">
            Mouser 다운로드 URL 직접 Import
          </h2>
          <p className="text-xs text-slate-500 dark:text-slate-400">
            정상 Chrome 세션에서 복사한 다운로드 URL과 Cookie header를 1회성으로 사용합니다. Cookie는 저장하지 않습니다.
          </p>
        </div>
        <div className="mt-4 grid gap-4 md:grid-cols-2">
          <label className="block">
            <span className="mb-1 block text-xs font-semibold text-slate-600 dark:text-slate-300">
              다운로드 URL들
            </span>
            <textarea
              value={downloadUrls}
              onChange={(event) => setDownloadUrls(event.target.value)}
              placeholder="https://www.mouser.kr/c/...?...&download=..."
              rows={6}
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 font-mono text-xs dark:border-slate-700 dark:bg-slate-950"
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs font-semibold text-slate-600 dark:text-slate-300">
              Cookie header
            </span>
            <textarea
              value={cookieHeader}
              onChange={(event) => setCookieHeader(event.target.value)}
              placeholder="akacd_Default_PR=...; other_cookie=..."
              rows={6}
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 font-mono text-xs dark:border-slate-700 dark:bg-slate-950"
            />
          </label>
        </div>
        <div className="mt-4 flex items-center justify-between gap-3">
          <div className="text-xs text-slate-500 dark:text-slate-400">
            URL은 공백/줄바꿈으로 구분합니다.
          </div>
          <button
            onClick={importDownloadUrls}
            disabled={!downloadUrls.trim() || !cookieHeader.trim() || downloadLoading}
            className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 transition-colors hover:bg-slate-50 disabled:cursor-not-allowed disabled:text-slate-400 disabled:hover:bg-transparent dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800 dark:disabled:text-slate-600"
          >
            {downloadLoading ? "다운로드 중..." : "URL 다운로드 후 Import"}
          </button>
        </div>
      </section>

      {error && (
        <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
          {error}
        </div>
      )}

      {result && (
        <section className="rounded-xl border border-emerald-200 bg-emerald-50 p-5 dark:border-emerald-900 dark:bg-emerald-950/30">
          <h2 className="text-sm font-semibold text-emerald-900 dark:text-emerald-100">
            Import 결과
          </h2>
          <div className="mt-3 grid grid-cols-2 gap-3 md:grid-cols-5">
            <Metric label="읽은 row" value={formatNum(result.totals.rowsRead)} />
            <Metric label="신규" value={formatNum(result.totals.inserted)} />
            <Metric label="업데이트" value={formatNum(result.totals.updated)} />
            <Metric label="스킵" value={formatNum(result.totals.skipped)} />
            <Metric label="오류 파일" value={formatNum(result.totals.errors)} />
          </div>
          <div className="mt-4 overflow-hidden rounded-lg border border-emerald-200 bg-white dark:border-emerald-900 dark:bg-slate-950">
            <table className="w-full text-sm">
              <thead className="bg-emerald-100/70 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-100">
                <tr>
                  <th className="px-3 py-2 text-left font-medium">파일</th>
                  <th className="px-3 py-2 text-right font-medium">row</th>
                  <th className="px-3 py-2 text-right font-medium">신규</th>
                  <th className="px-3 py-2 text-right font-medium">업데이트</th>
                  <th className="px-3 py-2 text-left font-medium">오류</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                {result.files.map((file) => (
                  <tr key={file.fileName}>
                    <td className="px-3 py-2 font-mono text-xs">{file.fileName}</td>
                    <td className="px-3 py-2 text-right">{formatNum(file.rowsRead)}</td>
                    <td className="px-3 py-2 text-right">{formatNum(file.inserted)}</td>
                    <td className="px-3 py-2 text-right">{formatNum(file.updated)}</td>
                    <td className="px-3 py-2 text-xs text-red-600 dark:text-red-300">
                      {file.errors.join(" / ")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section>
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-lg font-semibold">최근 Import 제품</h2>
          <span className="text-xs text-slate-400">
            마지막 업데이트: {formatDate(summary?.lastUpdated ?? null)}
          </span>
        </div>
        <div className="overflow-hidden rounded-xl border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-slate-600 dark:bg-slate-800/50 dark:text-slate-300">
              <tr>
                <th className="px-4 py-3 text-left font-medium">부품번호</th>
                <th className="px-4 py-3 text-left font-medium">제조사</th>
                <th className="px-4 py-3 text-left font-medium">설명</th>
                <th className="px-4 py-3 text-left font-medium">재고/가격</th>
                <th className="px-4 py-3 text-right font-medium">링크</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {(summary?.recent ?? []).length === 0 && (
                <tr>
                  <td
                    colSpan={5}
                    className="px-4 py-10 text-center text-slate-400 dark:text-slate-500"
                  >
                    아직 import된 제품이 없습니다.
                  </td>
                </tr>
              )}
              {(summary?.recent ?? []).map((product) => (
                <tr
                  key={product.id}
                  className="hover:bg-slate-50 dark:hover:bg-slate-800/50"
                >
                  <td className="px-4 py-3">
                    <div className="font-mono text-xs">
                      {product.manufacturer_part_number ||
                        product.mouser_part_number ||
                        "-"}
                    </div>
                    {product.mouser_part_number && (
                      <div className="mt-1 font-mono text-[11px] text-slate-400">
                        {product.mouser_part_number}
                      </div>
                    )}
                  </td>
                  <td className="px-4 py-3">{product.manufacturer ?? "-"}</td>
                  <td className="max-w-lg px-4 py-3">
                    <div className="line-clamp-2">{product.description ?? "-"}</div>
                  </td>
                  <td className="px-4 py-3 text-xs text-slate-500 dark:text-slate-400">
                    <div>{product.availability ?? "-"}</div>
                    {product.price && <div className="mt-1">{product.price}</div>}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <div className="inline-flex gap-3 text-xs">
                      {product.product_url && (
                        <a
                          href={product.product_url}
                          target="_blank"
                          rel="noreferrer"
                          className="text-slate-600 hover:text-slate-900 dark:text-slate-300 dark:hover:text-slate-100"
                        >
                          제품
                        </a>
                      )}
                      {product.datasheet_url && (
                        <a
                          href={product.datasheet_url}
                          target="_blank"
                          rel="noreferrer"
                          className="text-blue-600 hover:text-blue-800 dark:text-blue-400 dark:hover:text-blue-300"
                        >
                          datasheet
                        </a>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white px-3 py-2 dark:border-slate-800 dark:bg-slate-950">
      <div className="text-[11px] font-medium text-slate-500 dark:text-slate-400">
        {label}
      </div>
      <div className="mt-0.5 font-mono text-lg font-semibold text-slate-900 dark:text-slate-100">
        {value}
      </div>
    </div>
  );
}
