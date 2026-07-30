import type { Metadata } from "next"
import Link from "next/link"
import "./globals.css"

export const metadata: Metadata = {
  title: {
    default: "Libertree | 세계 문서 서가",
    template: "%s | Libertree",
  },
  description: "전 세계 공공·연구·학술 문서를 탐색하는 읽기 전용 서가",
}

const TreeRing = (): React.JSX.Element => (
  <svg aria-hidden="true" className="tree-ring" viewBox="0 0 48 48">
    <circle cx="24" cy="24" r="18" />
    <circle cx="24" cy="24" r="11" />
    <path d="M24 5v38M5 24h38" />
    <path d="M24 24c6-7 13-8 18-7-1 7-6 14-18 18" />
  </svg>
)

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ko">
      <body>
        <header className="masthead">
          <div className="frame masthead__inner">
            <Link className="brand" href="/" aria-label="Libertree 홈">
              <TreeRing />
              <span>Libertree</span>
            </Link>
            <nav aria-label="주요 탐색">
              <Link className="nav-link" href="/">둘러보기</Link>
              <Link className="nav-link" href="/search">검색</Link>
            </nav>
            <p className="read-only">읽기 전용 서가</p>
          </div>
        </header>
        <main className="frame main-content">{children}</main>
        <footer className="frame footer">
          <span>Libertree</span>
          <span>세계 문서 서가</span>
        </footer>
      </body>
    </html>
  )
}
