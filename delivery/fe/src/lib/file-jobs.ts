export type FileSummary = {
  total: number; queued: number; running: number; done: number; failed: number;
  skipped: number; cancelled: number; pdf_done: number; text_done: number; worker_alive: boolean;
  text_quality_review?: number;
  items: { seq_id: number; status: string; pdf_status: string; text_status: string; error: string | null; attempts: number;
    text_quality?: { state: string; fidelity_verified: boolean } | null }[];
};
export const filesActive = (files?: FileSummary): boolean => Boolean(files && files.queued + files.running > 0);
export const fileErrorLabel = (code: string | null): string => ({
  no_pdf_url: "PDF 주소 없음", invalid_pdf: "PDF 형식이 아닌 응답", file_too_large: "파일 크기 제한 초과",
  download_failed: "PDF 다운로드 실패", parse_failed: "텍스트 해석 실패", no_text_layer: "추출할 텍스트 없음 · OCR 필요 여부 확인",
  stage_timeout: "처리 시간 초과", disk_space_low: "파일 저장 공간 부족", worker_interrupted: "워커 중단 후 재시도",
  file_processing_failed: "파일 처리 실패", document_missing: "문서 정보 없음", stage_failed: "파일 처리 프로그램 오류",
  pdf_copy_restricted: "원본 PDF의 텍스트 복사 제한", pdf_password_required: "PDF 열기 암호 필요",
  text_quality_review_required: "추출문 문자 손상 · 원문 대조 필요", text_size_limit: "추출문 크기 제한 초과",
  text_extractor_unavailable: "대체 텍스트 추출기 없음",
}[code ?? ""] ?? (code ? "파일 처리 오류" : ""));
