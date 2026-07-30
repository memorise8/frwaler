import Link from "next/link";

export default function InternalReviewDocumentNotFound() {
  return (
    <div className="rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 px-6 py-16 text-center space-y-4">
      <h1 className="text-lg font-semibold text-slate-900 dark:text-slate-100">자료를 찾을 수 없습니다.</h1>
      <p className="text-sm text-slate-500 dark:text-slate-400">삭제되었거나 검토 대상에 없는 자료입니다.</p>
      <Link href="/search" className="inline-flex text-sm text-blue-600 dark:text-blue-400 hover:underline">
        자료 서가로 돌아가기
      </Link>
    </div>
  );
}
