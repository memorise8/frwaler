import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import FileProgress from "@/components/file-progress";
import { describe, expect, it, vi, afterEach } from "vitest";
vi.mock("server-only", () => ({}));
import { filesActive, fileErrorLabel, type FileSummary } from "@/lib/file-jobs";
import { POST } from "@/app/api/jobs/route";
import { POST as fileAction } from "@/app/api/jobs/[jobId]/files/[action]/route";

afterEach(() => vi.unstubAllGlobals());
describe("file processing", () => {
  it("continues tracking after metadata completes while files remain", () => {
    expect(filesActive(undefined)).toBe(false);
    expect(filesActive({ queued: 1, running: 0 } as FileSummary)).toBe(true);
    expect(filesActive({ queued: 0, running: 1 } as FileSummary)).toBe(true);
    expect(filesActive({ queued: 0, running: 0, failed: 3 } as FileSummary)).toBe(false);
  });
  it("distinguishes unavailable PDF and missing text layer from completion", () => {
    expect(fileErrorLabel("no_pdf_url")).toBe("PDF 주소 없음");
    expect(fileErrorLabel("no_text_layer")).toContain("OCR");
    expect(fileErrorLabel("stage_timeout")).toContain("시간 초과");
    expect(fileErrorLabel("pdf_copy_restricted")).toContain("복사 제한");
    expect(fileErrorLabel("text_quality_review_required")).toContain("원문 대조");
    expect(fileErrorLabel("internal details should not leak")).toBe("파일 처리 오류");
  });
  it("forwards the explicit file option to the backend", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: 12 }), { status: 200 }));
    vi.stubGlobal("fetch", fetcher);
    const response = await POST(new Request("http://localhost/api/jobs", { method: "POST", body: JSON.stringify({ siteId: "doaj-org-search", mode: "incremental", limit: 3, processFiles: true }) }));
    expect(response.status).toBe(201);
    expect(JSON.parse(fetcher.mock.calls[0][1].body).process_files).toBe(true);
  });
  it("does not enable files for legacy clients and rejects malformed options", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: 12 }), { status: 200 }));
    vi.stubGlobal("fetch", fetcher);
    await POST(new Request("http://localhost/api/jobs", { method: "POST", body: JSON.stringify({ siteId: "doaj-org-search", limit: 3 }) }));
    expect(JSON.parse(fetcher.mock.calls[0][1].body).process_files).toBe(false);
    const response = await POST(new Request("http://localhost/api/jobs", { method: "POST", body: JSON.stringify({ processFiles: "false" }) }));
    expect(response.status).toBe(400);
  });
  it("rejects arbitrary file actions without contacting the backend", async () => {
    const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    const response = await fileAction(new Request("http://localhost"), { params: Promise.resolve({ jobId: "12", action: "execute" }) });
    expect(response.status).toBe(422); expect(fetcher).not.toHaveBeenCalled();
  });
});


describe("file progress rendering", () => {
  const files: FileSummary = { total: 3, queued: 1, running: 0, done: 0, failed: 1,
    skipped: 1, cancelled: 0, pdf_done: 1, text_done: 0, worker_alive: false,
    items: [{ seq_id: 42, status: "failed", pdf_status: "done", text_status: "failed", error: "no_text_layer", attempts: 1 }] };
  it("separates metadata completion from pending files and exposes recovery controls", () => {
    const html = renderToStaticMarkup(createElement(FileProgress, { jobId: 1, metadataStatus: "done", files, onChange: () => {} }));
    expect(html).toContain("PDF 확보 1건");
    expect(html).toContain("텍스트 확보 0건");
    expect(html).toContain("실패·중지한 파일 재시도");
    expect(html).toContain("파일 처리 중지");
    expect(html).toContain("워커의 응답이 없습니다");
    expect(html).toContain("OCR 필요 여부 확인");
  });
  it("does not offer file actions while metadata is still collecting", () => {
    const html = renderToStaticMarkup(createElement(FileProgress, { jobId: 1, metadataStatus: "running", files, onChange: () => {} }));
    expect(html).not.toContain("실패·중지한 파일 재시도");
    expect(html).toContain("문서 수집이 끝난 후");
  });
  it("shows quality review independently of successful text extraction", () => {
    const withQuality: FileSummary = { ...files, queued: 0, done: 1, failed: 0, text_done: 1,
      text_quality_review: 1, items: [{ seq_id: 42, status: "done", pdf_status: "done",
        text_status: "done", error: null, attempts: 1,
        text_quality: { state: "review", fidelity_verified: false } }] };
    const html = renderToStaticMarkup(createElement(FileProgress, { jobId: 1, metadataStatus: "done", files: withQuality, onChange: () => {} }));
    expect(html).toContain("텍스트 확보 1건");
    expect(html).toContain("원문 대조가 필요한 추출문 1건");
    expect(html).toContain("문자 품질 검토 필요");
  });
});
