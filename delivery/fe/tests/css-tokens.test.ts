import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const css = (): string => readFileSync(new URL("../src/app/globals.css", import.meta.url), "utf8");

/**
 * Custom properties that are legitimately declared outside this stylesheet —
 * currently none. Anything added here must be provably set on <html> or <body>
 * by application code, not merely assumed to exist.
 */
const EXTERNALLY_PROVIDED = new Set<string>([]);

describe("globals.css custom properties", () => {
  it("defines every custom property it references", () => {
    const source = css();
    const rootBlock = /:root\s*\{([^}]*)\}/.exec(source)?.[1] ?? "";
    const defined = new Set([...rootBlock.matchAll(/(--[\w-]+)\s*:/g)].map((match) => match[1]));
    const referenced = [...source.matchAll(/var\(\s*(--[\w-]+)\s*[,)]/g)].map((match) => match[1]);
    const missing = [...new Set(referenced.filter((name) => !defined.has(name) && !EXTERNALLY_PROVIDED.has(name)))].sort();

    expect(missing).toEqual([]);
  });

  it("keeps a :root block that actually declares tokens", () => {
    const rootBlock = /:root\s*\{([^}]*)\}/.exec(css())?.[1] ?? "";
    expect(rootBlock).toContain("--rule:");
    expect(rootBlock).toContain("--moss:");
    expect(rootBlock).toContain("--canvas:");
  });
});
