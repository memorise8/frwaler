import { readFile, readdir } from "node:fs/promises";
import { resolve } from "node:path";
import { getHandoffConfiguration } from "../src/config/handoff-config.mjs";
import { getDashboardFixture } from "../src/fixtures/dashboard-fixture.mjs";
import { renderDocument } from "../src/render.mjs";

const rootDirectory = resolve(".");
const sourceDirectory = resolve(rootDirectory, "src");
const stylesDirectory = resolve(sourceDirectory, "styles");
const forbiddenFragments = [
  "fino" + "law",
  "@" + "/lib",
  "f" + "etch(",
  "http" + "://",
  "https" + "://",
  "NEXT" + "_PUBLIC_",
  "process" + ".env"
];

const listFiles = async (directory) => {
  const entries = await readdir(directory, { withFileTypes: true });
  const children = await Promise.all(
    entries.map(async (entry) => {
      const path = resolve(directory, entry.name);
      return entry.isDirectory() ? listFiles(path) : [path];
    })
  );
  return children.flat();
};

const fixture = getDashboardFixture();
if (fixture.dataClassification !== "합성 예시 데이터 · 비민감 정보") {
  throw new Error("Fixture data must be explicitly synthetic and non-sensitive.");
}
if (fixture.sectionLabels.materialOverview !== "도서 소개") {
  throw new Error("Fixture data must provide the future material overview field.");
}

const handoff = getHandoffConfiguration();
if (handoff.kind !== "absent") {
  throw new Error("The default handoff configuration must stay absent.");
}

const document = renderDocument();
for (const match of document.matchAll(/<a\b[^>]*\bhref="([^"]*)"/gi)) {
  if (!match[1].startsWith("#")) {
    throw new Error("An absent handoff configuration must not render an outbound anchor.");
  }
}

const countMatches = (source, expression) => (source.match(expression) ?? []).length;
const forbiddenDocumentPatterns = [
  /PDF 요약/i,
  /AI 요약/i,
  /<form\b/i,
  /<input\b/i,
  /<button\b/i,
  /fetch\(/i,
  /XMLHttpRequest/i,
  /localStorage/i
];

if (countMatches(document, /<h1\b/gi) !== 1) {
  throw new Error("The dashboard must render exactly one h1.");
}
const headingLevels = [...document.matchAll(/<h([1-6])\b/gi)].map((match) => Number(match[1]));
if (headingLevels.some((level, index) => index > 0 && level > headingLevels[index - 1] + 1)) {
  throw new Error("Dashboard headings must not skip levels.");
}
if (!/<nav\b/i.test(document) || !/<main\b/i.test(document)) {
  throw new Error("The dashboard must render navigation and main landmarks.");
}
if (!/<caption>/i.test(document) || countMatches(document, /<th scope="col">/gi) !== 4) {
  throw new Error("The exceptions table must have a caption and scoped headers.");
}
if (!/<th scope="row">/i.test(document)) {
  throw new Error("Exception rows must identify their row headers.");
}
for (const pattern of forbiddenDocumentPatterns) {
  if (pattern.test(document)) {
    throw new Error(`Forbidden dashboard behavior or claim: ${pattern}`);
  }
}

for (const variant of ["empty", "partial"]) {
  const variantDocument = renderDocument(variant);
  if (!variantDocument.includes(getDashboardFixture(variant).stateNotice)) {
    throw new Error(`${variant} fixture must render a readable state notice.`);
  }
}

const sourceFiles = await listFiles(sourceDirectory);
for (const sourceFile of sourceFiles) {
  const text = await readFile(sourceFile, "utf8");
  for (const fragment of forbiddenFragments) {
    if (text.includes(fragment)) {
      throw new Error(`Forbidden coupling in ${sourceFile}: ${fragment}`);
    }
  }
}

const stylesheet = [
  await readFile(resolve(stylesDirectory, "tokens.css"), "utf8"),
  await readFile(resolve(stylesDirectory, "base.css"), "utf8"),
  await readFile(resolve(stylesDirectory, "dashboard.css"), "utf8")
].join("\n");
const requiredStyleFragments = [
  "--canvas:",
  "--focus-blue:",
  "a:focus-visible",
  ".table-scroll:focus-visible",
  ".table-scroll {\n  overflow-x: auto;",
  ".table-scroll-hint {\n    display: block;",
  "@media (max-width: 375px)"
];
for (const fragment of requiredStyleFragments) {
  if (!stylesheet.includes(fragment)) {
    throw new Error(`Missing required responsive style rule: ${fragment}`);
  }
}
if (!/class="table-scroll"[^>]*tabindex="0"/.test(document)) {
  throw new Error("The exceptions table must be inside a keyboard-focusable scroll wrapper.");
}
if (!document.includes("표를 좌우로 밀어 전체 항목 보기")) {
  throw new Error("The mobile table-scroll hint must be rendered beside the exceptions table.");
}

console.log("Static project contract passed.");
