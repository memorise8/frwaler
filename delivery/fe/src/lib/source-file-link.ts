export type SourceFileLink = Readonly<{ href: string; extension: string | null; isPdf: boolean }>;

const SAFE_PROTOCOLS: ReadonlySet<string> = new Set(["http:", "https:"]);

// source.pdf_url is scraped from a third-party page -- it is not a value
// this service produced, and it can be malformed, relative, or (in
// principle) something like a javascript: URI. It must only ever reach an
// <a href> after parsing as an absolute http(s) URL, so nothing untrusted
// can become a live link on the page.
//
// The .pdf extension check is informational only, not a gate: a source can
// genuinely link a non-PDF file (document 536092 links an .xlsx), and that
// link is still useful to an operator who has no other route to the file.
// So a non-.pdf source file still returns a link -- callers label it
// differently rather than hiding it.
export const resolveSourceFileLink = (pdfUrl: string | null): SourceFileLink | null => {
  if (!pdfUrl) return null;
  let parsed: URL;
  try {
    parsed = new URL(pdfUrl);
  } catch {
    return null;
  }
  if (!SAFE_PROTOCOLS.has(parsed.protocol)) return null;
  const match = /\.([a-z0-9]+)$/i.exec(parsed.pathname);
  const extension = match ? match[1]!.toLowerCase() : null;
  return { href: parsed.toString(), extension, isPdf: extension === "pdf" };
};

export const sourceFileLinkLabel = (link: SourceFileLink): string => {
  if (link.isPdf) return "원문 PDF 열기 ↗";
  return link.extension ? `원문 파일 열기 ↗ (.${link.extension})` : "원문 파일 열기 ↗";
};
