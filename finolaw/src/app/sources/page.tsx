import { SourcesViewer } from "@/components/sources-viewer";

export const dynamic = "force-dynamic";

export default function SourcesPage() {
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-bold">원천 데이터</h1>
        <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
          papers.db (SQLite) 와 세법MD 파일 트리를 나란히 탐색합니다.
        </p>
      </div>
      <SourcesViewer />
    </div>
  );
}
