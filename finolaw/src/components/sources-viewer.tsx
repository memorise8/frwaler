"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { marked } from "marked";

// ─── Types ────────────────────────────────────────────────────────────────────

interface TableSummary {
  name: string;
  rowCount: number;
}

interface OverviewData {
  tables: TableSummary[];
  dbSize: number;
  lastCrawled: string | null;
  path: string;
}

interface ColumnInfo {
  name: string;
  type: string;
  notnull: boolean;
  pk: boolean;
}

interface PaperRow {
  id: string;
  site_id: string;
  title: string | null;
  published_date: string | null;
  category: string | null;
  metadata: string | null;
  crawled_at: string;
  department: string | null;
  abstract: string | null;
}

interface RowsResponse {
  rows: PaperRow[];
  total: number;
  page: number;
  pageSize: number;
}

interface TreeNode {
  name: string;
  kind: "dir" | "file";
  relPath: string;
  count?: number | null;
  countLabel?: string;
}

interface TreeResponse {
  children: TreeNode[];
  relPath: string;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function formatBytes(bytes: number): string {
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1)} GB`;
  if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(1)} MB`;
  if (bytes >= 1e3) return `${(bytes / 1e3).toFixed(1)} KB`;
  return `${bytes} B`;
}

function formatNum(n: number): string {
  return n.toLocaleString("ko-KR");
}

function formatDate(iso: string | null): string {
  if (!iso) return "-";
  try {
    return new Date(iso).toLocaleString("ko-KR", { dateStyle: "medium", timeStyle: "short" });
  } catch {
    return iso;
  }
}

function parseMetadata(raw: string | null): Record<string, unknown> {
  if (!raw) return {};
  try {
    return typeof raw === "object" ? (raw as Record<string, unknown>) : JSON.parse(raw as string);
  } catch {
    return {};
  }
}

const SITE_CHIPS = [
  { id: "", label: "전체" },
  { id: "nts-taxlaw-pd", label: "판례·심판 (pd)" },
  { id: "nts-taxlaw-qt", label: "질의회신 (qt)" },
  { id: "nts-taxlaw", label: "세법 (taxlaw)" },
];

// ─── Main Component ───────────────────────────────────────────────────────────

export function SourcesViewer() {
  // DB state
  const [overview, setOverview] = useState<OverviewData | null>(null);
  const [overviewError, setOverviewError] = useState<string | null>(null);
  const [activeTable, setActiveTable] = useState<string>("papers");
  const [columns, setColumns] = useState<ColumnInfo[]>([]);
  const [schemaOpen, setSchemaOpen] = useState(false);
  const [siteFilter, setSiteFilter] = useState<string>("");
  const [searchQ, setSearchQ] = useState<string>("");
  const [searchInput, setSearchInput] = useState<string>("");
  const [dbPage, setDbPage] = useState(1);
  const [rowsData, setRowsData] = useState<RowsResponse | null>(null);
  const [rowsLoading, setRowsLoading] = useState(false);
  const [selectedRow, setSelectedRow] = useState<Record<string, unknown> | null>(null);
  const [selectedRowId, setSelectedRowId] = useState<string | null>(null);
  const [rowDetailLoading, setRowDetailLoading] = useState(false);

  // MD state
  const [mdBreadcrumbs, setMdBreadcrumbs] = useState<{ label: string; path: string }[]>([]);
  const [mdTree, setMdTree] = useState<TreeNode[]>([]);
  const [mdTreeLoading, setMdTreeLoading] = useState(false);
  const [selectedMdFile, setSelectedMdFile] = useState<{ content: string; sizeBytes: number; basename: string; absolutePath: string } | null>(null);
  const [mdFileLoading, setMdFileLoading] = useState(false);
  const [mdHtml, setMdHtml] = useState<string>("");

  // Cross-link state
  const [crossLoading, setCrossLoading] = useState(false);
  const [crossResult, setCrossResult] = useState<{ docNumber: string; dbRows: unknown[]; mdPaths: string[] } | null>(null);

  const PAGE_SIZE = 50;
  const searchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // ── Fetch overview on mount ───────────────────────────────────────────────

  useEffect(() => {
    fetch("/api/sources/db/overview")
      .then((r) => r.json())
      .then((d) => {
        if (d.error) setOverviewError(d.error);
        else setOverview(d as OverviewData);
      })
      .catch((e) => setOverviewError(String(e)));
  }, []);

  // ── Fetch schema when table changes ──────────────────────────────────────

  useEffect(() => {
    if (!activeTable) return;
    fetch(`/api/sources/db/schema?table=${encodeURIComponent(activeTable)}`)
      .then((r) => r.json())
      .then((d) => {
        if (d.columns) setColumns(d.columns as ColumnInfo[]);
      })
      .catch(() => setColumns([]));
  }, [activeTable]);

  // ── Fetch rows ────────────────────────────────────────────────────────────

  const fetchRows = useCallback(async (table: string, site: string, q: string, page: number) => {
    setRowsLoading(true);
    try {
      const params = new URLSearchParams({ table, page: String(page), pageSize: String(PAGE_SIZE) });
      if (site) params.set("siteId", site);
      if (q) params.set("q", q);
      const res = await fetch(`/api/sources/db/rows?${params}`);
      const data = await res.json() as RowsResponse;
      setRowsData(data);
    } catch {
      setRowsData(null);
    } finally {
      setRowsLoading(false);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    fetchRows(activeTable, siteFilter, searchQ, dbPage);
  }, [activeTable, siteFilter, searchQ, dbPage, fetchRows]);

  // ── Load row detail on click ──────────────────────────────────────────────

  const loadRowDetail = useCallback(async (id: string, table: string) => {
    if (selectedRowId === id) {
      setSelectedRow(null);
      setSelectedRowId(null);
      return;
    }
    setSelectedRowId(id);
    setRowDetailLoading(true);
    try {
      const res = await fetch(`/api/sources/db/row?table=${encodeURIComponent(table)}&id=${encodeURIComponent(id)}`);
      const data = await res.json() as { row: Record<string, unknown> };
      setSelectedRow(data.row ?? null);
    } catch {
      setSelectedRow(null);
    } finally {
      setRowDetailLoading(false);
    }
  }, [selectedRowId]);

  // ── MD tree load ──────────────────────────────────────────────────────────

  const loadMdTree = useCallback(async (relPath: string, breadcrumbLabel?: string) => {
    setMdTreeLoading(true);
    try {
      const res = await fetch(`/api/sources/md/tree?path=${encodeURIComponent(relPath)}`);
      const data = await res.json() as TreeResponse;
      setMdTree(data.children ?? []);
      if (breadcrumbLabel !== undefined) {
        if (relPath === "") {
          setMdBreadcrumbs([]);
        } else {
          setMdBreadcrumbs((prev) => {
            const existingIdx = prev.findIndex((b) => b.path === relPath);
            if (existingIdx >= 0) return prev.slice(0, existingIdx + 1);
            return [...prev, { label: breadcrumbLabel, path: relPath }];
          });
        }
      }
    } catch {
      setMdTree([]);
    } finally {
      setMdTreeLoading(false);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    loadMdTree("", undefined);
  }, [loadMdTree]);

  // ── MD file load ──────────────────────────────────────────────────────────

  const loadMdFile = useCallback(async (relPath: string) => {
    setMdFileLoading(true);
    setSelectedMdFile(null);
    setMdHtml("");
    try {
      const res = await fetch(`/api/sources/md/file?path=${encodeURIComponent(relPath)}`);
      const data = await res.json() as { content: string; sizeBytes: number; basename: string; absolutePath: string };
      setSelectedMdFile(data);
      const html = await marked.parse(data.content, { gfm: true });
      setMdHtml(html);
    } catch {
      setSelectedMdFile(null);
    } finally {
      setMdFileLoading(false);
    }
  }, []);

  // ── Cross-link: DB row → MD ───────────────────────────────────────────────

  const crossLinkFromRow = useCallback(async () => {
    if (!selectedRow) return;
    const meta = selectedRow.metadata as Record<string, unknown> | null;
    const docNumber = meta?.documentNumber as string | undefined;
    if (!docNumber) {
      alert("이 행의 metadata에 documentNumber가 없습니다.");
      return;
    }
    setCrossLoading(true);
    setCrossResult(null);
    try {
      const res = await fetch(`/api/sources/cross?docNumber=${encodeURIComponent(docNumber)}`);
      const data = await res.json() as { docNumber: string; dbRows: unknown[]; mdPaths: string[] };
      setCrossResult(data);
      if (data.mdPaths.length > 0) {
        await loadMdFile(data.mdPaths[0]);
        // Navigate MD tree to show context
        const parts = data.mdPaths[0].split("/");
        if (parts.length >= 2) {
          await loadMdTree(parts[0], parts[0]);
        }
      }
    } finally {
      setCrossLoading(false);
    }
  }, [selectedRow, loadMdFile, loadMdTree]);

  // ── Cross-link: MD file → DB ──────────────────────────────────────────────

  const crossLinkFromMd = useCallback(async () => {
    if (!selectedMdFile) return;
    const docNumber = selectedMdFile.basename.replace(/\.md$/, "");
    setCrossLoading(true);
    setCrossResult(null);
    try {
      const res = await fetch(`/api/sources/cross?docNumber=${encodeURIComponent(docNumber)}`);
      const data = await res.json() as { docNumber: string; dbRows: unknown[]; mdPaths: string[] };
      setCrossResult(data);
      if (data.dbRows.length > 0) {
        const firstRow = data.dbRows[0] as PaperRow;
        setActiveTable("papers");
        setSiteFilter("");
        setSearchQ(docNumber);
        setSearchInput(docNumber);
        setDbPage(1);
        // Load full detail
        await loadRowDetail(firstRow.id, "papers");
      }
    } finally {
      setCrossLoading(false);
    }
  }, [selectedMdFile, loadRowDetail]);

  // ── Search debounce ───────────────────────────────────────────────────────

  const handleSearchChange = (value: string) => {
    setSearchInput(value);
    if (searchTimer.current) clearTimeout(searchTimer.current);
    searchTimer.current = setTimeout(() => {
      setSearchQ(value);
      setDbPage(1);
    }, 400);
  };

  // ── Reset page when filters change ────────────────────────────────────────

  const handleSiteFilter = (site: string) => {
    setSiteFilter(site);
    setDbPage(1);
  };

  const handleTableChange = (table: string) => {
    setActiveTable(table);
    setDbPage(1);
    setSelectedRow(null);
    setSelectedRowId(null);
  };

  const totalPages = rowsData ? Math.max(1, Math.ceil(rowsData.total / PAGE_SIZE)) : 1;

  // ─── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="flex gap-4 h-[calc(100vh-200px)] min-h-[600px]">
      {/* ═══ LEFT PANE — DB ═════════════════════════════════════════════════ */}
      <div className="lg:w-1/2 w-full flex flex-col gap-3 overflow-hidden">
        {/* Overview bar */}
        <div className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 p-4 shrink-0">
          {overviewError ? (
            <div className="text-red-600 dark:text-red-400 text-sm font-medium">{overviewError}</div>
          ) : !overview ? (
            <div className="text-slate-400 text-sm animate-pulse">DB 연결 중…</div>
          ) : (
            <div className="space-y-1">
              <div className="flex items-center justify-between gap-2 flex-wrap">
                <span className="font-mono text-xs text-slate-500 dark:text-slate-400 truncate">{overview.path}</span>
                <span className="text-xs font-semibold text-slate-700 dark:text-slate-300">{formatBytes(overview.dbSize)}</span>
              </div>
              <div className="flex flex-wrap gap-3 text-xs text-slate-600 dark:text-slate-400">
                {overview.tables.map((t) => (
                  <span key={t.name}>
                    <span className="font-mono">{t.name}</span>{" "}
                    <span className="text-slate-400">({formatNum(t.rowCount)}행)</span>
                  </span>
                ))}
              </div>
              <div className="text-xs text-slate-400">최근 크롤링: {formatDate(overview.lastCrawled)}</div>
            </div>
          )}
        </div>

        {/* Table tabs */}
        <div className="flex gap-1 shrink-0 flex-wrap">
          {["papers", "sites", "products", "doc_index"].map((t) => (
            <button
              key={t}
              onClick={() => handleTableChange(t)}
              className={`px-3 py-1.5 rounded-lg text-xs font-mono font-medium transition-colors ${
                activeTable === t
                  ? "bg-blue-600 text-white"
                  : "bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800"
              }`}
            >
              {t}
            </button>
          ))}
        </div>

        {/* Schema panel */}
        <div className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 shrink-0">
          <button
            className="w-full flex items-center justify-between px-4 py-2.5 text-xs font-medium text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800/50 rounded-xl"
            onClick={() => setSchemaOpen((v) => !v)}
          >
            <span>스키마 — {activeTable}</span>
            <span>{schemaOpen ? "▲" : "▼"}</span>
          </button>
          {schemaOpen && (
            <div className="border-t border-slate-100 dark:border-slate-800 px-4 pb-3">
              <table className="w-full text-xs mt-2">
                <thead className="text-slate-500 dark:text-slate-400">
                  <tr>
                    <th className="text-left py-1 pr-3 font-medium">컬럼</th>
                    <th className="text-left py-1 pr-3 font-medium">타입</th>
                    <th className="text-left py-1 font-medium">속성</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-50 dark:divide-slate-800/50">
                  {columns.map((c) => (
                    <tr key={c.name}>
                      <td className="py-1 pr-3 font-mono text-slate-800 dark:text-slate-200">{c.name}</td>
                      <td className="py-1 pr-3 text-slate-500 dark:text-slate-400">{c.type || "—"}</td>
                      <td className="py-1 text-slate-400 space-x-1">
                        {c.pk && <span className="px-1 bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300 rounded text-[10px]">PK</span>}
                        {c.notnull && <span className="px-1 bg-slate-100 dark:bg-slate-800 rounded text-[10px]">NOT NULL</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Site filter chips + search — papers only */}
        {activeTable === "papers" && (
          <div className="flex flex-col gap-2 shrink-0">
            <div className="flex flex-wrap gap-1.5">
              {SITE_CHIPS.map((chip) => (
                <button
                  key={chip.id}
                  onClick={() => handleSiteFilter(chip.id)}
                  className={`px-2.5 py-1 rounded-full text-xs font-medium transition-colors ${
                    siteFilter === chip.id
                      ? "bg-indigo-600 text-white"
                      : "bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 text-slate-600 dark:text-slate-300 hover:border-indigo-300 dark:hover:border-indigo-700"
                  }`}
                >
                  {chip.label}
                </button>
              ))}
            </div>
            <input
              type="text"
              value={searchInput}
              onChange={(e) => handleSearchChange(e.target.value)}
              placeholder="제목·본문 검색 (FTS5)"
              className="px-3 py-2 text-sm border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-900 text-slate-900 dark:text-slate-100 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
        )}

        {/* Row count */}
        <div className="text-xs text-slate-500 dark:text-slate-400 shrink-0">
          {rowsData
            ? `${formatNum(rowsData.total)}건 · ${dbPage} / ${totalPages} 페이지`
            : rowsLoading
            ? "로딩 중…"
            : ""}
        </div>

        {/* Rows table */}
        <div className="flex-1 overflow-auto bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800">
          {rowsLoading && !rowsData ? (
            <div className="p-6 text-center text-slate-400 text-sm animate-pulse">행 로딩 중…</div>
          ) : !rowsData || rowsData.rows.length === 0 ? (
            <div className="p-6 text-center text-slate-400 text-sm">결과 없음</div>
          ) : (
            <table className="w-full text-xs">
              <thead className="bg-slate-50 dark:bg-slate-800/50 sticky top-0 z-10">
                <tr>
                  {activeTable === "papers" ? (
                    <>
                      <th className="text-left px-3 py-2 font-medium text-slate-600 dark:text-slate-300 whitespace-nowrap">제목</th>
                      <th className="text-left px-3 py-2 font-medium text-slate-600 dark:text-slate-300 whitespace-nowrap">유형</th>
                      <th className="text-left px-3 py-2 font-medium text-slate-600 dark:text-slate-300 whitespace-nowrap">날짜</th>
                      <th className="text-left px-3 py-2 font-medium text-slate-600 dark:text-slate-300 whitespace-nowrap">사이트</th>
                    </>
                  ) : (
                    columns.slice(0, 5).map((c) => (
                      <th key={c.name} className="text-left px-3 py-2 font-medium text-slate-600 dark:text-slate-300 whitespace-nowrap">{c.name}</th>
                    ))
                  )}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                {(rowsData.rows as PaperRow[]).map((row) => {
                  const meta = parseMetadata(row.metadata);
                  const isSelected = selectedRowId === row.id;
                  return (
                    <>
                      <tr
                        key={row.id}
                        onClick={() => loadRowDetail(row.id, activeTable)}
                        className={`cursor-pointer transition-colors ${
                          isSelected
                            ? "bg-blue-50 dark:bg-blue-950/40"
                            : "hover:bg-slate-50 dark:hover:bg-slate-800/50"
                        }`}
                      >
                        {activeTable === "papers" ? (
                          <>
                            <td className="px-3 py-2 max-w-[240px]">
                              <span className="block truncate text-slate-800 dark:text-slate-200 font-medium">
                                {row.title || "(제목 없음)"}
                              </span>
                              {typeof meta.documentNumber === "string" && meta.documentNumber && (
                                <span className="font-mono text-slate-400 text-[10px]">{meta.documentNumber}</span>
                              )}
                            </td>
                            <td className="px-3 py-2 text-slate-500 dark:text-slate-400 whitespace-nowrap">
                              {typeof meta.documentTypeName === "string" ? meta.documentTypeName : "-"}
                            </td>
                            <td className="px-3 py-2 text-slate-500 dark:text-slate-400 whitespace-nowrap">
                              {row.published_date || "-"}
                            </td>
                            <td className="px-3 py-2 font-mono text-slate-400 whitespace-nowrap text-[10px]">
                              {row.site_id}
                            </td>
                          </>
                        ) : (
                          columns.slice(0, 5).map((c) => (
                            <td key={c.name} className="px-3 py-2 text-slate-700 dark:text-slate-300 max-w-[160px]">
                              <span className="block truncate">
                                {String((row as unknown as Record<string, unknown>)[c.name] ?? "")}
                              </span>
                            </td>
                          ))
                        )}
                      </tr>
                      {isSelected && (
                        <tr key={`${row.id}-detail`}>
                          <td colSpan={4} className="px-4 py-4 bg-blue-50 dark:bg-blue-950/30 border-b border-blue-100 dark:border-blue-900/40">
                            {rowDetailLoading ? (
                              <div className="text-xs text-slate-400 animate-pulse">로딩 중…</div>
                            ) : selectedRow ? (
                              <div className="space-y-3">
                                <div className="flex items-center gap-2 flex-wrap">
                                  <button
                                    onClick={(e) => { e.stopPropagation(); crossLinkFromRow(); }}
                                    disabled={crossLoading}
                                    className="px-3 py-1.5 bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white text-xs rounded-lg font-medium"
                                  >
                                    {crossLoading ? "검색 중…" : "MD 파일 찾기"}
                                  </button>
                                  {crossResult && crossResult.mdPaths.length > 0 && (
                                    <span className="text-xs text-emerald-600 dark:text-emerald-400">
                                      MD 발견: {crossResult.mdPaths[0]}
                                    </span>
                                  )}
                                  {crossResult && crossResult.mdPaths.length === 0 && (
                                    <span className="text-xs text-slate-400">MD 파일 없음</span>
                                  )}
                                </div>
                                <pre className="text-[11px] font-mono bg-slate-100 dark:bg-slate-900 rounded-lg p-3 overflow-auto max-h-64 text-slate-800 dark:text-slate-200 whitespace-pre-wrap break-all">
                                  {JSON.stringify(selectedRow, null, 2)}
                                </pre>
                              </div>
                            ) : null}
                          </td>
                        </tr>
                      )}
                    </>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>

        {/* Pagination */}
        {totalPages > 1 && (
          <div className="flex items-center gap-2 justify-center shrink-0">
            <button
              onClick={() => setDbPage((p) => Math.max(1, p - 1))}
              disabled={dbPage <= 1}
              className="px-3 py-1.5 rounded-lg border border-slate-300 dark:border-slate-700 text-xs disabled:opacity-40 hover:bg-slate-50 dark:hover:bg-slate-800"
            >
              ← 이전
            </button>
            <span className="text-xs text-slate-500 dark:text-slate-400">
              {dbPage} / {totalPages}
            </span>
            <button
              onClick={() => setDbPage((p) => Math.min(totalPages, p + 1))}
              disabled={dbPage >= totalPages}
              className="px-3 py-1.5 rounded-lg border border-slate-300 dark:border-slate-700 text-xs disabled:opacity-40 hover:bg-slate-50 dark:hover:bg-slate-800"
            >
              다음 →
            </button>
          </div>
        )}
      </div>

      {/* ═══ RIGHT PANE — MD Tree ═══════════════════════════════════════════ */}
      <div className="lg:w-1/2 w-full flex flex-col gap-3 overflow-hidden">
        {/* Breadcrumb */}
        <div className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 px-4 py-2.5 shrink-0 flex items-center gap-1.5 flex-wrap text-xs">
          <button
            onClick={() => { loadMdTree("", undefined); setMdBreadcrumbs([]); setSelectedMdFile(null); setMdHtml(""); }}
            className="text-blue-600 dark:text-blue-400 hover:underline font-medium"
          >
            세법MD
          </button>
          {mdBreadcrumbs.map((b, i) => (
            <span key={b.path} className="flex items-center gap-1.5">
              <span className="text-slate-400">/</span>
              <button
                onClick={() => loadMdTree(b.path, b.label)}
                className={i === mdBreadcrumbs.length - 1 ? "text-slate-700 dark:text-slate-300 font-medium" : "text-blue-600 dark:text-blue-400 hover:underline"}
              >
                {b.label}
              </button>
            </span>
          ))}
        </div>

        {/* MD file detail or tree */}
        {selectedMdFile ? (
          <div className="flex-1 overflow-auto flex flex-col gap-3">
            {/* File header */}
            <div className="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 px-4 py-3 shrink-0 space-y-1.5">
              <div className="flex items-center justify-between gap-2 flex-wrap">
                <span className="font-mono text-sm font-semibold text-slate-800 dark:text-slate-200">
                  {selectedMdFile.basename}
                </span>
                <span className="text-xs text-slate-400">{formatBytes(selectedMdFile.sizeBytes)}</span>
              </div>
              <div className="font-mono text-[11px] text-slate-400 dark:text-slate-500 break-all select-all">
                {selectedMdFile.absolutePath}
              </div>
              <div className="flex gap-2 flex-wrap">
                <button
                  onClick={() => { setSelectedMdFile(null); setMdHtml(""); }}
                  className="px-2.5 py-1 text-xs border border-slate-300 dark:border-slate-700 rounded-lg hover:bg-slate-50 dark:hover:bg-slate-800"
                >
                  ← 트리로 돌아가기
                </button>
                <button
                  onClick={() => crossLinkFromMd()}
                  disabled={crossLoading}
                  className="px-2.5 py-1 text-xs bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 text-white rounded-lg font-medium"
                >
                  {crossLoading ? "검색 중…" : "DB 행 보기"}
                </button>
                {crossResult && crossResult.dbRows.length > 0 && (
                  <span className="text-xs text-emerald-600 dark:text-emerald-400 self-center">
                    DB 행 {crossResult.dbRows.length}건 발견
                  </span>
                )}
              </div>
            </div>

            {/* Rendered markdown */}
            <div className="flex-1 overflow-auto bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 p-5">
              {mdFileLoading ? (
                <div className="text-slate-400 text-sm animate-pulse">파일 로딩 중…</div>
              ) : (
                <div
                  className="prose-md-viewer"
                  dangerouslySetInnerHTML={{ __html: mdHtml }}
                />
              )}
            </div>
          </div>
        ) : (
          /* Tree view */
          <div className="flex-1 overflow-auto bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800">
            {mdTreeLoading ? (
              <div className="p-6 text-center text-slate-400 text-sm animate-pulse">디렉토리 로딩 중…</div>
            ) : mdTree.length === 0 ? (
              <div className="p-6 text-center text-slate-400 text-sm">항목 없음</div>
            ) : (
              <ul className="divide-y divide-slate-100 dark:divide-slate-800">
                {mdTree.map((node) => (
                  <li key={node.relPath}>
                    <button
                      className="w-full text-left px-4 py-2.5 flex items-center gap-3 hover:bg-slate-50 dark:hover:bg-slate-800/50 transition-colors"
                      onClick={() => {
                        if (node.kind === "dir") {
                          loadMdTree(node.relPath, node.name);
                        } else {
                          loadMdFile(node.relPath);
                        }
                      }}
                    >
                      <span className="shrink-0 text-base">
                        {node.kind === "dir" ? "📁" : "📄"}
                      </span>
                      <span className={`flex-1 text-sm ${node.kind === "file" ? "font-mono text-xs text-slate-600 dark:text-slate-400" : "font-medium text-slate-800 dark:text-slate-200"}`}>
                        {node.name}
                      </span>
                      {node.kind === "dir" && (
                        <span className="text-xs text-slate-400">
                          {node.countLabel ?? (node.count != null ? `${formatNum(node.count)}개` : "")}
                        </span>
                      )}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
