import { describe, expect, it } from "vitest";
import { pdfMissingReason, textMissingReason } from "../src/lib/document-file-state";

describe("pdfMissingReason", () => {
  it("names the source file's extension when it is not a PDF", () => {
    expect(pdfMissingReason("acma_pay_on-time_performance_report_2025-26.xlsx"))
      .toBe("원본이 PDF가 아닌 .xlsx 파일이라 PDF를 확보하지 않았습니다.");
  });

  it("is case-insensitive about the extension", () => {
    expect(pdfMissingReason("report.XLSX")).toBe("원본이 PDF가 아닌 .xlsx 파일이라 PDF를 확보하지 않았습니다.");
  });

  it("gives a generic reason when the source file is itself a PDF", () => {
    expect(pdfMissingReason("report.pdf")).toBe("원문에서 PDF를 아직 확보하지 못했습니다.");
  });

  it("gives a generic reason when there is no filename to inspect", () => {
    expect(pdfMissingReason(null)).toBe("원문에서 PDF를 아직 확보하지 못했습니다.");
  });

  it("gives a generic reason when the filename has no extension", () => {
    expect(pdfMissingReason("report")).toBe("원문에서 PDF를 아직 확보하지 못했습니다.");
  });
});

describe("textMissingReason", () => {
  it("explains that extraction failed when a PDF is stored", () => {
    expect(textMissingReason(true)).toBe("PDF는 있지만 텍스트를 추출하지 못했습니다.");
  });

  it("explains that there is nothing to extract from when no PDF is stored", () => {
    expect(textMissingReason(false)).toBe("PDF가 없어 텍스트를 추출하지 않았습니다.");
  });
});
