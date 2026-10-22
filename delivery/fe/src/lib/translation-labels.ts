// Plain-Korean names for the /translations operations panel. The BE codes
// (disk_free_bytes, idle …) stay available in the collapsed 기술 정보 block.

export const METRIC_LABELS: Readonly<Record<string, string>> = {
  endpoint_healthy: "번역 서버 응답",
  disk_free_bytes: "디스크 남은 공간",
  gpu_memory_free_bytes: "GPU 남은 메모리",
  gpu_utilization_percent: "GPU 사용률",
};

export const metricLabel = (code: string): string => METRIC_LABELS[code] ?? code;

const formatBytes = (value: number): string => {
  const gib = value / 1024 ** 3;
  return gib >= 1024 ? `${(gib / 1024).toFixed(1)} TiB` : `${gib.toFixed(1)} GiB`;
};

export const metricValue = (code: string, numeric: number | null, flag: boolean | null): string => {
  if (flag !== null) return flag ? "정상" : "응답 없음";
  if (numeric === null) return "-";
  if (code.endsWith("_bytes")) return formatBytes(numeric);
  if (code.endsWith("_percent")) return `${numeric.toFixed(0)}%`;
  return numeric.toLocaleString("ko-KR");
};

export const WORKER_STATUS_LABELS: Readonly<Record<string, string>> = {
  idle: "대기 중", busy: "작업 중", running: "작업 중", stopping: "멈추는 중", stopped: "멈춤",
};

export const workerStatusLabel = (code: string): string => WORKER_STATUS_LABELS[code] ?? code;
