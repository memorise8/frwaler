import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Crawler Dashboard",
  description: "Multi-site crawler management dashboard",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="bg-gray-50 min-h-screen">
        <nav className="bg-white border-b border-gray-200 px-6 py-3">
          <div className="max-w-7xl mx-auto flex items-center justify-between">
            <a href="/" className="text-xl font-bold text-gray-900">Crawler Dashboard</a>
            <div className="flex gap-4 text-sm">
              <a href="/smart-find" className="text-blue-600 hover:text-blue-800 font-medium">문서 찾기</a>
              <a href="/" className="text-gray-600 hover:text-gray-900">Dashboard</a>
              <a href="/sites" className="text-gray-600 hover:text-gray-900">Sites</a>
              <a href="/papers" className="text-gray-600 hover:text-gray-900">Papers</a>
              <a href="/jobs" className="text-gray-600 hover:text-gray-900">Jobs</a>
              <a href="/url-test" className="text-gray-600 hover:text-gray-900">URL Test</a>
              <a href="/auto-add" className="text-purple-600 hover:text-purple-800 font-medium">Auto-Add</a>
              <a href="/batch-add" className="text-purple-600 hover:text-purple-800 font-medium">일괄 추가</a>
              <a href="/reports" className="text-gray-600 hover:text-gray-900">리포트</a>
              <a href="/crawl-preview" className="text-gray-600 hover:text-gray-900">미리보기</a>
              <a href="/settings" className="text-gray-600 hover:text-gray-900">Settings</a>
            </div>
          </div>
        </nav>
        <main className="max-w-7xl mx-auto px-6 py-8">{children}</main>
      </body>
    </html>
  );
}
