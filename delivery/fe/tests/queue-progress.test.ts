import { describe, expect, it } from "vitest";
import { formatEta, parseQueueSummary } from "../src/lib/queue-progress";

const payload = (over: Record<string, unknown> = {}) => ({
  counts: { queued: 784, running: 1, cancelling: 0, done: 19, failed: 0, cancelled: 0 },
  active: 785, finished_24h: 19, avg_seconds: 22, samples: 19, ...over,
});

describe("parseQueueSummary", () => {
  it("names the two numbers an operator is actually waiting on", () => {
    const progress = parseQueueSummary(payload())!;
    expect(progress.queued).toBe(784);
    expect(progress.running).toBe(1);
    expect(progress.headline).toContain("784");
    expect(progress.headline).toContain("실행 중 1건");
  });

  // 작업 이력은 영구 보존이라 전체 대비 진행률은 지난 몇 달치에 희석된다.
  // 진행률은 이번 작업분(최근 24시간 완료 + 남은 것)에 대해서만 낸다.
  // 리터럴 2를 쓴다: 19/(19+785)=2.36%. 식을 다시 쓰면 구현을 그대로 베낀
  // 동어반복이 되어 "갓 시작한 수집이 2%로 보인다"는 요지를 검증하지 못한다.
  it("measures progress against the current sweep, not all of history", () => {
    const progress = parseQueueSummary(payload())!;
    expect(progress.percent).toBe(2);
  });

  it("has no progress bar when nothing is queued or running", () => {
    const progress = parseQueueSummary(payload({
      counts: { queued: 0, running: 0, cancelling: 0, done: 19, failed: 0, cancelled: 0 },
      active: 0,
    }))!;
    expect(progress.percent).toBeNull();
    expect(progress.etaText).toBeNull();
    expect(progress.headline).toBe("대기 중인 작업이 없습니다.");
  });

  it("counts a cancelling job as still occupying the worker", () => {
    const progress = parseQueueSummary(payload({
      counts: { queued: 0, running: 0, cancelling: 2, done: 0, failed: 0, cancelled: 0 },
      active: 2,
    }))!;
    expect(progress.active).toBe(2);
  });

  it("refuses a malformed payload rather than rendering NaN", () => {
    expect(parseQueueSummary(null)).toBeNull();
    expect(parseQueueSummary({})).toBeNull();
    expect(parseQueueSummary({ counts: "nope", active: 1 })).toBeNull();
    expect(parseQueueSummary(payload({ active: "many" }))).toBeNull();
  });

  it("refuses a fractional count", () => {
    expect(parseQueueSummary(payload({ active: 785.5 }))).toBeNull();
    expect(parseQueueSummary(payload({
      counts: { queued: 784.5, running: 1, cancelling: 0, done: 19, failed: 0, cancelled: 0 },
    }))).toBeNull();
  });

  // 평균이 실수라고 추정을 버리면 안 된다.
  it("keeps a fractional mean rather than discarding the estimate", () => {
    expect(parseQueueSummary(payload({ avg_seconds: 22.4 }))!.etaText).not.toBeNull();
  });
});

describe("formatEta", () => {
  // 워커는 한 번에 한 사이트씩 처리하는 단일 직렬 루프다. 따라서 남은 시간은
  // 남은 건수 x 평균 소요 시간이다.
  it("multiplies the mean run by the jobs still waiting", () => {
    // 22 x 785 = 17,270초 = 4시간 47분 50초 → 분은 반올림해 48
    expect(formatEta(22, 785)).toBe("약 4시간 48분 남음");
  });

  it("drops to minutes and then to a floor", () => {
    expect(formatEta(22, 100)).toBe("약 37분 남음");   // 2,200초 → 36.7분 → 37
    expect(formatEta(2, 10)).toBe("1분 미만 남음");     // 20초
  });

  // 표본이 모자라면 BE가 null을 준다. 그때 숫자를 지어내지 않는다.
  it("says nothing when there is no measurement to base it on", () => {
    expect(formatEta(null, 785)).toBeNull();
    expect(formatEta(22, 0)).toBeNull();
  });

  // 22초 평균에 327건 남은 상태는 804대 수집 중 평범한 값이다. 시·분을 먼저
  // 쪼갠 뒤 분을 반올림하면 여기서 "약 1시간 60분 남음"이 나왔다.
  it("never renders a 60-minute component", () => {
    expect(formatEta(22, 327)).toBe("약 2시간 남음");
    expect(formatEta(7199, 1)).toBe("약 2시간 남음");
    expect(formatEta(17999, 1)).toBe("약 5시간 남음");
  });

  // 평균 소요 시간은 실수가 정상이고(avg()::float8), 남은 건수는 정수여야 한다.
  it("accepts a fractional mean but not a fractional job count", () => {
    expect(formatEta(22.4, 10)).toBe("약 4분 남음");
    expect(formatEta(22, 10.5)).toBeNull();
  });
});
