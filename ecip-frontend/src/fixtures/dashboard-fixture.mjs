const content = Object.freeze({
  dataClassification: "합성 예시 데이터 · 비민감 정보",
  dashboardTitle: "자료 현황 대시보드",
  navigationLabel: "대시보드 구역",
  navigation: Object.freeze([
    Object.freeze({ label: "현황 개요", target: "overview" }),
    Object.freeze({ label: "도서 소개", target: "material-overview" }),
    Object.freeze({ label: "출처와 확인 항목", target: "exceptions" })
  ]),
  sectionLabels: Object.freeze({
    overview: "현재 현황",
    coverage: "수집 범위 읽기",
    materialOverview: "도서 소개",
    provenance: "출처와 기준",
    exceptions: "우선 확인 항목"
  }),
  emptyState: "현재 표시할 항목이 없습니다. 다음 갱신 시점에 다시 확인해 주세요.",
  partialState: "일부 항목은 확인 중입니다. 표시된 범위 안에서만 현황을 읽어 주세요.",
  table: Object.freeze({
    caption: "우선 확인이 필요한 합성 예시 항목",
    headers: Object.freeze(["우선순위", "확인 대상", "현재 상태", "안내"])
  })
});

const defaultDashboardFixture = Object.freeze({
  ...content,
  snapshot: Object.freeze({
    label: "기준 시점",
    value: "2026년 7월 23일 09:00",
    context: "등록 전 화면 구조 검토용 현재 자료 현황"
  }),
  coverage: Object.freeze([
    Object.freeze({ label: "등록 대상", value: "832건", detail: "목록에 반영된 합성 대상 수", ratio: "832 / 832" }),
    Object.freeze({ label: "소개 제공", value: "694건", detail: "도서 소개가 준비된 합성 대상 수", ratio: "694 / 832" }),
    Object.freeze({ label: "확인 필요", value: "138건", detail: "소개 또는 출처 확인이 필요한 합성 대상 수", ratio: "138 / 832" })
  ]),
  materialOverview: Object.freeze({
    title: "지역 기록과 생활 자료를 한눈에 읽는 안내",
    text: "등록 전 정보 구조를 검토하기 위한 합성 소개문입니다. 자료의 주제와 활용 맥락을 간단히 안내합니다.",
    metadata: "주제: 지역 생활 · 제공 범위: 합성 예시"
  }),
  provenance: Object.freeze([
    Object.freeze({ label: "자료 범위", value: "등록 검토용 합성 목록" }),
    Object.freeze({ label: "갱신 기준", value: "정해진 검토 시점의 단일 스냅샷" }),
    Object.freeze({ label: "표시 원칙", value: "확인된 범위와 확인 필요 항목을 함께 표시" })
  ]),
  exceptions: Object.freeze([
    Object.freeze({ priority: "높음", subject: "소개 미확인 자료", status: "확인 필요", guidance: "소개문 제공 여부를 확인해 주세요." }),
    Object.freeze({ priority: "보통", subject: "출처 표기 검토", status: "검토 중", guidance: "표시 기준을 정리한 뒤 반영해 주세요." }),
    Object.freeze({ priority: "낮음", subject: "갱신 시점 안내", status: "안내 필요", guidance: "다음 검토 시점을 함께 알려 주세요." })
  ])
});

const partialDashboardFixture = Object.freeze({
  ...defaultDashboardFixture,
  snapshot: Object.freeze({
    ...defaultDashboardFixture.snapshot,
    context: "일부 자료가 확인 중인 합성 예시 스냅샷"
  }),
  coverage: Object.freeze([
    defaultDashboardFixture.coverage[0],
    defaultDashboardFixture.coverage[1],
    Object.freeze({ label: "확인 필요", value: "138건", detail: "일부 출처 정보가 아직 확인 중인 합성 대상 수", ratio: "138 / 832" })
  ]),
  stateNotice: content.partialState
});

const emptyDashboardFixture = Object.freeze({
  ...content,
  snapshot: Object.freeze({
    label: "기준 시점",
    value: "표시할 스냅샷 없음",
    context: "등록 전 빈 데이터 상태를 확인하기 위한 합성 예시"
  }),
  coverage: Object.freeze([]),
  materialOverview: null,
  provenance: Object.freeze([]),
  exceptions: Object.freeze([]),
  stateNotice: content.emptyState
});

const fixtures = Object.freeze({
  default: defaultDashboardFixture,
  partial: partialDashboardFixture,
  empty: emptyDashboardFixture
});

export const getDashboardFixture = (variant = "default") => fixtures[variant] ?? fixtures.default;
