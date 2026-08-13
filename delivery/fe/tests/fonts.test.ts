import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const read = (relative: string): string => readFileSync(new URL(relative, import.meta.url), "utf8");

describe("web font pipeline", () => {
  it("declares both families through next/font", () => {
    const fonts = read("../src/app/fonts.ts");
    expect(fonts).toContain('from "next/font/google"');
    expect(fonts).toContain("Noto_Sans_KR");
    expect(fonts).toContain("Noto_Serif_KR");
    expect(fonts).toContain('variable: "--font-sans"');
    expect(fonts).toContain('variable: "--font-serif"');
    expect(fonts).toContain('display: "swap"');
  });

  it("mounts the font variables on the html element", () => {
    const layout = read("../src/app/layout.tsx");
    expect(layout).toMatch(/sansKR\.variable/);
    expect(layout).toMatch(/serifKR\.variable/);
  });

  it("routes the design tokens through the loaded families", () => {
    const css = read("../src/app/globals.css");
    expect(css).toMatch(/--sans:\s*var\(--font-sans\)/);
    expect(css).toMatch(/--serif:\s*var\(--font-serif\)/);
    expect(css).toMatch(/body\{[^}]*font-family:var\(--sans\)/);
  });

  it("no longer names unloaded families directly in the stylesheet", () => {
    const css = read("../src/app/globals.css");
    expect(css).not.toContain('"Noto Sans KR"');
    expect(css).not.toContain('"Noto Serif KR"');
  });
});
