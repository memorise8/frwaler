"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { isActiveNav, NAV_ITEMS } from "@/lib/nav-items";

export const SiteNav = () => {
  const pathname = usePathname();

  return (
    <nav aria-label="운영 메뉴">
      {NAV_ITEMS.map((item) => {
        const active = isActiveNav(pathname, item.match);
        return (
          <Link
            aria-current={active ? "page" : undefined}
            className={active ? "nav-link nav-link-active" : "nav-link"}
            href={item.href}
            key={item.href}
          >
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
};
