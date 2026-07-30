import { readFile } from "node:fs/promises";

const raw = await readFile("data/catalog.json", "utf8");
const rows = JSON.parse(raw);

if (!Array.isArray(rows)) {
  throw new Error("catalogue must be an array");
}

const allowed = new Set(["authors", "introduction", "introductionLabel", "journal", "keywords", "originalUrl", "pdfUrl", "publicId", "publishedDate", "publisher", "sourceHost", "sourceName", "title"]);
for (const row of rows) {
  for (const key of Object.keys(row)) {
    if (!allowed.has(key)) {
      throw new Error(`unexpected catalogue field: ${key}`);
    }
  }
}

const source = await readFile("src/app.js", "utf8");
if (!source.includes('target="_blank" rel="noopener noreferrer"')) {
  throw new Error("outbound links need safe new-tab attributes");
}

for (const requiredFragment of [
  "#/source/${encodeURIComponent(source)}",
  "?source=${encodeURIComponent(selectedSource)}",
  "decodeRouteSegment(sourceMatch[1] ?? \"\")",
]) {
  if (!source.includes(requiredFragment)) {
    throw new Error(`source browsing must preserve safely encoded hash routes: ${requiredFragment}`);
  }
}

console.log(`catalogue shape OK (${rows.length} records); external links and source routes are safely configured`);
