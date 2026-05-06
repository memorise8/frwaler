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
  title: "Finolaw — 세법 문서 허브",
  description: "국세법령 판례/해석례 통합 검색 및 크롤러 관리 콘솔",
};

const NAV = [
  { href: "/", label: "대시보드", icon: "📊" },
  { href: "/search", label: "세법 검색", icon: "🔍" },
  { href: "/smart-find", label: "URL 크롤링", icon: "🌐" },
  { href: "/auto-add", label: "Auto-Add", icon: "🧠" },
  { href: "/crawler", label: "크롤러 관리", icon: "⚙️" },
  { href: "/products/import", label: "제품 Import", icon: "📦" },
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
          <div className="max-w-7xl mx-auto px-6 py-3 flex items-center gap-6">
            <Link href="/" className="flex items-center gap-2 font-bold text-lg">
              <span className="text-2xl">⚖️</span>
              <span>Finolaw</span>
            </Link>
            <nav className="flex items-center gap-1 flex-1">
              {NAV.map((item) => (
                <Link
                  key={item.href}
                  href={item.href}
                  className="px-3 py-1.5 rounded-lg text-sm font-medium text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800 hover:text-slate-900 dark:hover:text-slate-100 transition-colors"
                >
                  <span className="mr-1.5">{item.icon}</span>
                  {item.label}
                </Link>
              ))}
            </nav>
            <span className="text-xs text-slate-400 dark:text-slate-500 font-mono">
              port 3001
            </span>
            <ThemeToggle />
          </div>
        </header>
        <main className="flex-1 max-w-7xl w-full mx-auto px-6 py-8">
          {children}
        </main>
        <footer className="border-t border-slate-200 dark:border-slate-800 py-4 text-center text-xs text-slate-400 dark:text-slate-500">
          Finolaw · 국세법령 통합 · powered by crawler-poc
        </footer>
      </body>
    </html>
  );
}
