import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const css = (): string => readFileSync(new URL("../src/app/globals.css", import.meta.url), "utf8");

/**
 * Custom properties that are legitimately declared outside this stylesheet.
 * Anything added here must be provably set on <html> or <body> by
 * application code, not merely assumed to exist.
 */
const EXTERNALLY_PROVIDED = new Set<string>([
  // Set on <html> by next/font via sansKR.variable / serifKR.variable — see src/app/fonts.ts.
  "--font-sans",
  "--font-serif",
]);

/**
 * Every :root block's declaration body, in source order. A stylesheet can
 * legitimately declare :root more than once (for example a
 * `@media (prefers-color-scheme: dark)` override block), and tokens defined
 * only in a later block must still count as "defined".
 */
const rootBlocks = (source: string): string[] =>
  [...source.matchAll(/:root\s*\{([^}]*)\}/g)].map((match) => match[1]);

describe("globals.css custom properties", () => {
  it("defines every custom property it references", () => {
    const source = css();
    const defined = new Set(
      rootBlocks(source).flatMap((block) => [...block.matchAll(/(--[\w-]+)\s*:/g)].map((match) => match[1])),
    );
    const referenced = [...source.matchAll(/var\(\s*(--[\w-]+)\s*[,)]/g)].map((match) => match[1]);
    const missing = [...new Set(referenced.filter((name) => !defined.has(name) && !EXTERNALLY_PROVIDED.has(name)))].sort();

    expect(missing).toEqual([]);
  });

  it("keeps a :root block that actually declares tokens", () => {
    const blocks = rootBlocks(css());
    // Documents the current shape: exactly one :root block. If a second one
    // is added later, this assertion should fail loudly so the change is a
    // deliberate decision, not a silent behaviour shift caught only by the
    // guard above misfiring for an unrelated reason.
    expect(blocks).toHaveLength(1);

    const rootBlock = blocks[0] ?? "";
    expect(rootBlock).toContain("--rule:");
    expect(rootBlock).toContain("--moss:");
    expect(rootBlock).toContain("--canvas:");
  });
});
