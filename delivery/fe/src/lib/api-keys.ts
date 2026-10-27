// 설정 → API 키: BE /settings/api-keys 의 줄과 「시험 후 저장」 결과를 화면 문구로 바꾼다.
// 키 값은 이 화면이 받지도 보이지도 않는다(BE 는 쓰기 전용).

export type ApiKeyLastTest = { result: string; reason: string | null; at: string };
export type ApiKeyRow = {
  provider: string;
  label: string;
  testable: boolean;
  test_method: string;
  configured: boolean | null;
  last_test: ApiKeyLastTest | null;
};
export type ApiKeyList = { available: boolean; providers: ApiKeyRow[] };

export const API_KEY_PROVIDER_RE = /^[a-z0-9-]{1,40}$/;
// BE 가 받는 몸통 한도(delivery/be/api_keys.py MAX_BODY)와 같다.
export const API_KEY_MAX_BODY = 2048;
export const API_KEY_MAX_LENGTH = 256;

const FAILURE_MESSAGES: Record<string, string> = {
  bad_key: "키가 맞지 않습니다. 다시 입력해 주세요.",
  invalid_format: "키 형식이 맞지 않습니다. 다시 입력해 주세요.",
  rate_limited: "한도 초과입니다. 잠시 뒤 다시 시도해 주세요.",
  too_many_attempts: "시도가 너무 많습니다. 10분 뒤 다시 시도해 주세요.",
  connect_failed: "연결 실패입니다. 다시 시도해 주세요.",
  refused: "공급자가 요청을 거절했습니다. 잠시 뒤 다시 시도해 주세요.",
  busy: "다른 키를 시험하는 중입니다. 잠시 뒤 다시 시도해 주세요.",
  error: "저장하지 못했습니다. 다시 시도해 주세요.",
  not_supported: "이 공급자는 아직 시험 방법이 준비되지 않아 저장할 수 없습니다.",
  unconfigured: "키 보관 폴더가 설정되지 않아 저장할 수 없습니다. 관리자에게 문의해 주세요.",
};

const LAST_TEST_LABELS: Record<string, string> = {
  saved: "정상 · 저장됨",
  bad_key: "실패 · 키가 맞지 않음",
  rate_limited: "실패 · 한도 초과",
  connect_failed: "실패 · 연결 실패",
  refused: "실패 · 공급자 거절",
};

export const API_KEY_NETWORK_ERROR = "연결 실패입니다. 다시 시도해 주세요.";

export const describeApiKeyFailure = (status: number, body: unknown): string => {
  const reason = typeof body === "object" && body !== null ? (body as { reason?: unknown }).reason : undefined;
  if (typeof reason === "string" && FAILURE_MESSAGES[reason]) return FAILURE_MESSAGES[reason];
  if (status === 401 || status === 503) return "운영자 인증 또는 서버 설정 문제로 저장하지 못했습니다.";
  if (status === 413 || status === 400) return "요청 형식이 올바르지 않습니다. 다시 입력해 주세요.";
  return "저장하지 못했습니다. 다시 시도해 주세요.";
};

export const configuredLabel = (row: ApiKeyRow): string =>
  row.configured === null ? "알 수 없음" : row.configured ? "설정됨" : "없음";

export const lastTestLabel = (row: ApiKeyRow): string => {
  if (!row.testable) return "시험 준비 중";
  if (!row.last_test) return "시험 기록 없음";
  return LAST_TEST_LABELS[row.last_test.result] ?? `실패 · ${row.last_test.result}`;
};

export const usable = (row: ApiKeyRow): boolean => row.configured === true && row.last_test?.result === "saved";
