"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";

const links = [
  { href: "/", label: "홈" },
  { href: "/products", label: "제품 검색" },
  { href: "/categories", label: "카테고리" },
  { href: "/stats", label: "통계" },
  { href: "/screening", label: "Screening" },
];

export default function NavBar() {
  const pathname = usePathname();

  return (
    <header
      style={{
        background: "rgba(8, 12, 26, 0.85)",
        borderBottom: "1px solid var(--border-dim)",
        backdropFilter: "blur(12px)",
        WebkitBackdropFilter: "blur(12px)",
        position: "sticky",
        top: 0,
        zIndex: 100,
      }}
    >
      <div className="max-w-7xl mx-auto px-6 h-14 flex items-center justify-between">
        {/* Logo */}
        <Link href="/" className="flex items-center gap-3 group">
          {/* SVG chip icon */}
          <svg
            width="28"
            height="28"
            viewBox="0 0 28 28"
            fill="none"
            xmlns="http://www.w3.org/2000/svg"
            style={{ flexShrink: 0 }}
          >
            <rect x="8" y="8" width="12" height="12" rx="1.5" stroke="#00c8f0" strokeWidth="1.5" />
            <rect x="10.5" y="10.5" width="7" height="7" rx="0.75" fill="rgba(0,200,240,0.15)" stroke="#00c8f0" strokeWidth="1" />
            {/* Pins left */}
            <line x1="3" y1="11" x2="8" y2="11" stroke="#00c8f0" strokeWidth="1.5" strokeLinecap="round" />
            <line x1="3" y1="14" x2="8" y2="14" stroke="#00c8f0" strokeWidth="1.5" strokeLinecap="round" />
            <line x1="3" y1="17" x2="8" y2="17" stroke="#00c8f0" strokeWidth="1.5" strokeLinecap="round" />
            {/* Pins right */}
            <line x1="20" y1="11" x2="25" y2="11" stroke="#00c8f0" strokeWidth="1.5" strokeLinecap="round" />
            <line x1="20" y1="14" x2="25" y2="14" stroke="#00c8f0" strokeWidth="1.5" strokeLinecap="round" />
            <line x1="20" y1="17" x2="25" y2="17" stroke="#00c8f0" strokeWidth="1.5" strokeLinecap="round" />
            {/* Pins top */}
            <line x1="11" y1="3" x2="11" y2="8" stroke="#00c8f0" strokeWidth="1.5" strokeLinecap="round" />
            <line x1="14" y1="3" x2="14" y2="8" stroke="#00c8f0" strokeWidth="1.5" strokeLinecap="round" />
            <line x1="17" y1="3" x2="17" y2="8" stroke="#00c8f0" strokeWidth="1.5" strokeLinecap="round" />
            {/* Pins bottom */}
            <line x1="11" y1="20" x2="11" y2="25" stroke="#00c8f0" strokeWidth="1.5" strokeLinecap="round" />
            <line x1="14" y1="20" x2="14" y2="25" stroke="#00c8f0" strokeWidth="1.5" strokeLinecap="round" />
            <line x1="17" y1="20" x2="17" y2="25" stroke="#00c8f0" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
          <div className="flex flex-col leading-none">
            <span
              style={{
                color: "var(--text-primary)",
                fontSize: "0.9rem",
                fontWeight: 700,
                letterSpacing: "0.04em",
              }}
            >
              우주 부품 탐색기
            </span>
            <span
              style={{
                color: "var(--text-dim)",
                fontSize: "0.65rem",
                letterSpacing: "0.08em",
                fontFamily: "'DM Mono', monospace",
                textTransform: "uppercase",
              }}
            >
              Space Components Explorer
            </span>
          </div>
        </Link>

        {/* Navigation */}
        <nav className="flex items-center gap-6">
          {links.map(({ href, label }) => {
            const isActive =
              href === "/" ? pathname === "/" : pathname.startsWith(href);
            return (
              <Link
                key={href}
                href={href}
                className={`nav-link ${isActive ? "active" : ""}`}
              >
                {label}
              </Link>
            );
          })}

          {/* Live indicator */}
          <div
            className="flex items-center gap-2 ml-2"
            style={{
              borderLeft: "1px solid var(--border-dim)",
              paddingLeft: "1rem",
            }}
          >
            <div className="pulse-dot" />
            <span
              style={{
                color: "var(--text-dim)",
                fontSize: "0.7rem",
                fontFamily: "'DM Mono', monospace",
                letterSpacing: "0.05em",
              }}
            >
              LIVE
            </span>
          </div>
        </nav>
      </div>
    </header>
  );
}
