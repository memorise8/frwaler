"use client"

import { useCallback, useEffect, useState } from "react"

type DirEntry = { readonly type: "dir"; readonly name: string; readonly relPath: string }
type FileEntry = { readonly type: "file"; readonly name: string; readonly size: number; readonly ext: string; readonly seqId: number | null; readonly blobHref: string | null }
type Entry = DirEntry | FileEntry
type FilesResponse = { readonly path: string; readonly isRoot: boolean; readonly entries: readonly Entry[]; readonly total: number; readonly offset: number; readonly limit: number }

const formatSize = (bytes: number): string => {
  if (bytes < 1024) return `${bytes} B`
  const kb = bytes / 1024
  if (kb < 1024) return `${kb.toFixed(1)} KB`
  return `${(kb / 1024).toFixed(1)} MB`
}

const breadcrumbSegments = (path: string): readonly { readonly label: string; readonly relPath: string }[] => {
  const parts = path.split("/").filter(Boolean)
  const crumbs: { readonly label: string; readonly relPath: string }[] = []
  let accumulated = ""
  for (const part of parts) {
    accumulated = accumulated === "" ? part : `${accumulated}/${part}`
    crumbs.push({ label: part, relPath: accumulated })
  }
  return crumbs
}

const parentPath = (path: string): string => {
  const parts = path.split("/").filter(Boolean)
  parts.pop()
  return parts.join("/")
}

const readPathFromLocation = (): string =>
  typeof window === "undefined" ? "" : new URLSearchParams(window.location.search).get("path") ?? ""

export default function FilesPage(): React.JSX.Element {
  const [path, setPath] = useState<string>("")
  const [data, setData] = useState<FilesResponse | null>(null)
  const [entries, setEntries] = useState<readonly Entry[]>([])
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setPath(readPathFromLocation())
    const onPopState = (): void => setPath(readPathFromLocation())
    window.addEventListener("popstate", onPopState)
    return () => window.removeEventListener("popstate", onPopState)
  }, [])

  const navigateTo = useCallback((relPath: string): void => {
    const values = new URLSearchParams()
    if (relPath !== "") values.set("path", relPath)
    const query = values.toString()
    window.history.pushState(null, "", query === "" ? "/files" : `/files?${query}`)
    setPath(relPath)
  }, [])

  const load = useCallback(async (targetPath: string): Promise<void> => {
    setLoading(true)
    setError(null)
    setData(null)
    setEntries([])
    try {
      const response = await fetch(`/api/files?path=${encodeURIComponent(targetPath)}`)
      if (response.status === 401) {
        setError("인증이 필요합니다. 페이지를 새로고침해 다시 로그인해 주세요.")
        return
      }
      if (response.status === 404) {
        setError("경로를 찾을 수 없습니다.")
        return
      }
      if (!response.ok) {
        setError("파일 목록을 불러오지 못했습니다.")
        return
      }
      const body = await response.json() as FilesResponse
      setData(body)
      setEntries(body.entries)
    } catch {
      setError("파일 목록을 불러오지 못했습니다.")
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load(path)
  }, [path, load])

  const loadMore = async (): Promise<void> => {
    if (data === null) return
    setLoadingMore(true)
    try {
      const response = await fetch(`/api/files?path=${encodeURIComponent(path)}&offset=${entries.length}`)
      if (!response.ok) return
      const body = await response.json() as FilesResponse
      setEntries((previous) => [...previous, ...body.entries])
      setData(body)
    } finally {
      setLoadingMore(false)
    }
  }

  const crumbs = breadcrumbSegments(path)
  const hasMore = data !== null && entries.length < data.total

  return (
    <section className="search-page stack">
      <div>
        <p className="page-label">STORAGE / BROWSE</p>
        <h1>파일 브라우저</h1>
        <p className="lede">서버에 저장된 원본 blob 디렉터리를 그대로 탐색합니다.</p>
      </div>
      <nav className="breadcrumb" aria-label="현재 위치">
        <button type="button" onClick={() => navigateTo("")}>libertree/</button>
        {crumbs.map((crumb) => <span key={crumb.relPath}>{" / "}<button type="button" onClick={() => navigateTo(crumb.relPath)}>{crumb.label}</button></span>)}
      </nav>
      {loading ? <p className="lede">불러오는 중…</p> : null}
      {error !== null ? <section className="empty-state"><h2>문제가 발생했습니다.</h2><p>{error}</p></section> : null}
      {!loading && error === null && data !== null ? (
        <>
          {!data.isRoot ? <button type="button" className="filter-reset" onClick={() => navigateTo(parentPath(data.path))}>.. 상위 디렉터리</button> : null}
          {entries.length === 0 ? (
            <section className="empty-state"><h2>빈 디렉터리입니다.</h2><p>이 위치에는 파일이나 하위 디렉터리가 없습니다.</p></section>
          ) : (
            <ul className="file-browser">
              {entries.map((entry) => entry.type === "dir" ? (
                <li key={`dir-${entry.relPath}`} className="file-browser__row">
                  <button type="button" className="file-browser__name" onClick={() => navigateTo(entry.relPath)}>📁 {entry.name}</button>
                </li>
              ) : (
                <li key={`file-${entry.name}`} className="file-browser__row">
                  {entry.blobHref !== null ? <a className="file-browser__name" href={entry.blobHref} target="_blank" rel="noopener noreferrer">📄 {entry.name}</a> : <span className="file-browser__name">📄 {entry.name}</span>}
                  <span className="file-browser__size">{formatSize(entry.size)}</span>
                </li>
              ))}
            </ul>
          )}
          {hasMore ? <button type="button" className="filter-reset" disabled={loadingMore} onClick={() => { void loadMore() }}>{loadingMore ? "불러오는 중…" : "더 보기"}</button> : null}
        </>
      ) : null}
    </section>
  )
}
