import { describe, expect, it } from "vitest";
import { resolveSourceFileLink, sourceFileLinkLabel } from "../src/lib/source-file-link";

describe("resolveSourceFileLink", () => {
  it("returns null when there is no pdf_url", () => {
    expect(resolveSourceFileLink(null)).toBeNull();
  });

  it("resolves a direct https PDF link", () => {
    expect(resolveSourceFileLink("https://example.gov/files/report.pdf")).toEqual({
      href: "https://example.gov/files/report.pdf",
      extension: "pdf",
      isPdf: true,
    });
  });

  it("resolves a non-PDF source file instead of hiding it", () => {
    expect(resolveSourceFileLink("https://www.acma.gov.au/sites/default/files/2026-08/acma_pay_on-time_performance_report_2025-26.xlsx"))
      .toEqual({
        href: "https://www.acma.gov.au/sites/default/files/2026-08/acma_pay_on-time_performance_report_2025-26.xlsx",
        extension: "xlsx",
        isPdf: false,
      });
  });

  it("lower-cases the detected extension", () => {
    const link = resolveSourceFileLink("https://example.gov/files/report.PDF");
    expect(link?.extension).toBe("pdf");
    expect(link?.isPdf).toBe(true);
  });

  it("ignores a query string when detecting the extension", () => {
    const link = resolveSourceFileLink("https://example.gov/files/report.pdf?download=1");
    expect(link?.extension).toBe("pdf");
  });

  it("returns a link with no extension when the path has none", () => {
    const link = resolveSourceFileLink("https://example.gov/files/report");
    expect(link).toEqual({ href: "https://example.gov/files/report", extension: null, isPdf: false });
  });

  it("rejects an unsafe protocol such as javascript:", () => {
    expect(resolveSourceFileLink("javascript:alert(1)")).toBeNull();
  });

  it("rejects an ftp: URL", () => {
    expect(resolveSourceFileLink("ftp://example.gov/report.pdf")).toBeNull();
  });

  it("rejects a value that does not parse as an absolute URL", () => {
    expect(resolveSourceFileLink("/relative/path/report.pdf")).toBeNull();
    expect(resolveSourceFileLink("not a url")).toBeNull();
  });
});

describe("sourceFileLinkLabel", () => {
  it("labels a PDF source file", () => {
    expect(sourceFileLinkLabel({ href: "https://x/y.pdf", extension: "pdf", isPdf: true })).toBe("원문 PDF 열기 ↗");
  });

  it("labels a non-PDF source file with its extension", () => {
    expect(sourceFileLinkLabel({ href: "https://x/y.xlsx", extension: "xlsx", isPdf: false }))
      .toBe("원문 파일 열기 ↗ (.xlsx)");
  });

  it("falls back to a generic label when no extension is known", () => {
    expect(sourceFileLinkLabel({ href: "https://x/y", extension: null, isPdf: false })).toBe("원문 파일 열기 ↗");
  });
});
