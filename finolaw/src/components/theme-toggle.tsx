"use client";

import { useEffect, useState } from "react";

type Theme = "light" | "dark";

function applyTheme(theme: Theme) {
  const root = document.documentElement;
  if (theme === "dark") root.classList.add("dark");
  else root.classList.remove("dark");
  root.dataset.theme = theme;
}

function readStoredTheme(): Theme {
  try {
    const stored = localStorage.getItem("finolaw-theme");
    if (stored === "dark" || stored === "light") return stored;
  } catch {
    // ignore
  }
  return typeof window !== "undefined" &&
    window.matchMedia?.("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>("light");
  const [mountedTheme, setMountedTheme] = useState<Theme | null>(null);

  useEffect(() => {
    const id = window.requestAnimationFrame(() => {
      const current = readStoredTheme();
      setTheme(current);
      setMountedTheme(current);
      applyTheme(current);
    });
    return () => window.cancelAnimationFrame(id);
  }, []);

  const toggle = () => {
    const next: Theme = theme === "dark" ? "light" : "dark";
    setTheme(next);
    setMountedTheme(next);
    applyTheme(next);
    try {
      localStorage.setItem("finolaw-theme", next);
    } catch {
      // ignore
    }
  };

  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={theme === "dark" ? "라이트 모드로 전환" : "다크 모드로 전환"}
      title={theme === "dark" ? "라이트 모드" : "다크 모드"}
      className="w-9 h-9 flex items-center justify-center rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-700 transition-colors"
    >
      <span className="text-lg leading-none" suppressHydrationWarning>
        {mountedTheme ? (theme === "dark" ? "☀️" : "🌙") : "🌓"}
      </span>
    </button>
  );
}

/**
 * Inline script to set theme class BEFORE React hydrates — prevents FOUC.
 * Insert in <head> via dangerouslySetInnerHTML.
 */
export const THEME_INIT_SCRIPT = `
(function() {
  try {
    var stored = localStorage.getItem('finolaw-theme');
    var theme = (stored === 'dark' || stored === 'light')
      ? stored
      : (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    if (theme === 'dark') document.documentElement.classList.add('dark');
    document.documentElement.dataset.theme = theme;
  } catch (e) {}
})();
`;
