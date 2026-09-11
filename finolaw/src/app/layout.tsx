import type { Metadata } from "next";
import Link from "next/link";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
import { ThemeToggle, THEME_INIT_SCRIPT } from "@/components/theme-toggle";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Libertree — 글로벌 문서 허브",
  description:
    "전세계 정부·연구·학술 사이트의 문서 통합 검색 및 수집 현황",
};

const NAV = [
  { href: "/", label: "대시보드", icon: "🌐" },
  { href: "/search", label: "문서 검색", icon: "🔍" },
  { href: "/admin/status", label: "수집 현황", icon: "📈" },
  { href: "/admin/summary", label: "요약 결과", icon: "🧠" },
  { href: "/smart-find", label: "URL 크롤링", icon: "🛰️" },
  { href: "/auto-add", label: "Auto-Add", icon: "✨" },
  { href: "/crawler-status", label: "크롤러 상태", icon: "🟢" },
  { href: "/crawler", label: "크롤러 관리", icon: "⚙️" },
];

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang="ko"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
      suppressHydrationWarning
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body className="min-h-full flex flex-col bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
        <header className="bg-white dark:bg-slate-900 border-b border-slate-200 dark:border-slate-800 sticky top-0 z-10">
          <div className="max-w-7xl mx-auto px-4 sm:px-6 py-3 flex flex-wrap items-center gap-3 sm:gap-6">
            <Link href="/" className="flex shrink-0 items-center gap-2 font-bold text-lg">
              <span className="text-2xl">🌳</span>
              <span>Libertree</span>
            </Link>
            <nav className="order-3 flex min-w-0 w-full gap-1 overflow-x-auto whitespace-nowrap pb-1 sm:order-none sm:w-auto sm:flex-1 sm:pb-0">
              {NAV.map((item) => (
                <Link
                  key={item.href}
                  href={item.href}
                  className="shrink-0 px-3 py-1.5 rounded-lg text-sm font-medium text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800 hover:text-slate-900 dark:hover:text-slate-100 transition-colors"
                >
                  <span className="mr-1.5">{item.icon}</span>
                  {item.label}
                </Link>
              ))}
            </nav>
            <span className="hidden sm:inline text-xs text-slate-400 dark:text-slate-500 font-mono">
              port 3001
            </span>
            <ThemeToggle />
          </div>
        </header>
        <main className="flex-1 min-w-0 max-w-7xl w-full mx-auto px-4 sm:px-6 py-6 sm:py-8">
          {children}
        </main>
        <footer className="border-t border-slate-200 dark:border-slate-800 py-4 text-center text-xs text-slate-400 dark:text-slate-500">
          Libertree · 글로벌 문서 허브 · libertree.db
        </footer>
      </body>
    </html>
  );
}
