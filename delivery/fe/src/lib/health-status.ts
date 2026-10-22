import { formatSeoul } from "@/lib/seoul-time";

export const HEALTH_LABELS: Readonly<Record<string, string>> = {
  healthy: "표본 수집 정상", partial: "일부 성공·확인 필요", empty: "0건·판정 보류",
  blocked: "접근 제한", network_error: "접속 오류", timeout: "시간 초과",
  configuration: "설정 필요", error: "실행 오류", unsupported: "별도 점검 필요",
  invalid_data: "필수 정보 누락", unchecked: "미검사", stale: "검사 오래됨",
  code_changed: "코드 변경·재검사 필요", report_error: "검사 기록 오류",
};

export const healthLabel = (state: string): string => HEALTH_LABELS[state] ?? "알 수 없음";
// Asia/Seoul via the shared helper, so server and browser render the same text.
export const formatHealthDate = (value: string | null): string => {
  if (!value) return "검사 기록 없음";
  return formatSeoul(value, { seconds: true, fallback: "시각 확인 불가" });
};

// Every site still "미검사": the per-site table carries no information yet.
export const allSitesUnchecked = (sites: readonly { probe: { state: string } }[]): boolean =>
  sites.length > 0 && sites.every((site) => site.probe.state === "unchecked");

export const healthTone = (state: string): "healthy" | "unhealthy" | "unknown" => {
  if (state === "healthy") return "healthy";
  return ["blocked", "network_error", "error", "invalid_data"].includes(state) ? "unhealthy" : "unknown";
};

export type HealthSite = {
  site_id: string; site_name: string; code_status: string; audit_status: string;
  resume_signals: boolean; time_budget_signal: boolean;
  probe: { state: string; checked_at: string | null; last_state?: string; sample_count?: number; elapsed_s?: number };
};
export type CrawlerHealthReport = {
  measured_at: string; stale_hours: number; sites: HealthSite[];
  registry_errors: { source: string; error: string }[];
  summary: { catalogue: number; code: Record<string, number>; probes: Record<string, number> };
};
export type SystemHealthReport = {
  database: string; schema: string; schema_issue: string | null;
  workers: { worker_id: string; alive: boolean; last_seen_at: string; crawl_started_at: string | null }[];
  queue: Record<string, number>; enabled_schedules?: number;
  blob_sample: { state: string; checked?: number; missing?: number; invalid?: number };
};
