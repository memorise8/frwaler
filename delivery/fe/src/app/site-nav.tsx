"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { isActiveNav, NAV_GROUPS } from "@/lib/nav-items";

export const SiteNav = () => {
  const pathname = usePathname();

  return (
    <nav aria-label="운영 메뉴" className="site-nav">
      {NAV_GROUPS.map((group) => (
        <div className="nav-group" key={group.label} role="group" aria-label={group.label}>
          <span className="nav-group-label" aria-hidden="true">{group.label}</span>
          <div className="nav-group-links">
            {group.items.map((item) => {
              const active = isActiveNav(pathname, item.match);
              return (
                <Link
                  aria-current={active ? "page" : undefined}
                  className={active ? "nav-link nav-link-active" : "nav-link"}
                  href={item.href}
                  key={item.href}
                  title={item.locked ? "별도 암호를 묻는 화면입니다." : undefined}
                >
                  {item.locked && <span aria-hidden="true">🔒 </span>}
                  {item.label}
                </Link>
              );
            })}
          </div>
        </div>
      ))}
    </nav>
  );
};
