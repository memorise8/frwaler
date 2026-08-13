import type { Metadata } from "next";
import Link from "next/link";
import { SiteNav } from "./site-nav";
import { sansKR, serifKR } from "./fonts";
import "./globals.css";

export const metadata: Metadata = {
  title: "Libertree 운영 콘솔",
  description: "Libertree 크롤러 상태 및 수집 운영 콘솔",
};

const TreeRing = () => (
  <svg aria-hidden="true" className="tree-ring" viewBox="0 0 48 48">
    <circle cx="24" cy="24" r="18" />
    <circle cx="24" cy="24" r="11" />
    <path d="M24 5v38M5 24h38" />
    <path d="M24 24c6-7 13-8 18-7-1 7-6 14-18 18" />
  </svg>
);

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html className={`${sansKR.variable} ${serifKR.variable}`} lang="ko">
      <body>
        <header className="masthead">
          <div className="frame masthead-inner">
            <Link className="brand" href="/" aria-label="Libertree 운영 콘솔 홈">
              <TreeRing />
              <span>Libertree</span>
            </Link>
            <SiteNav />
            <p className="console-mark">운영 콘솔 · MVP</p>
          </div>
        </header>
        <main className="frame main-content">{children}</main>
        <footer className="frame footer"><span>Libertree Delivery</span><span>크롤러 운영 콘솔</span></footer>
      </body>
    </html>
  );
}
