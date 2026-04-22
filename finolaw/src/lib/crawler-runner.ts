import { spawn } from "node:child_process";
import path from "node:path";
import fs from "node:fs";

const PROJECT_ROOT = path.join(process.cwd(), "..");
const PYTHON_BIN = path.join(PROJECT_ROOT, ".venv", "bin", "python");
const LOG_DIR = path.join(PROJECT_ROOT, ".cache");

export type CrawlerSiteId = string;

export interface RunCrawlerOptions {
  siteId: CrawlerSiteId;
  docType?: string;
  incremental?: boolean;
  limit?: number;
}

export interface RunningJob {
  jobId: string;
  pid: number;
  siteId: string;
  logPath: string;
  startedAt: string;
}

const jobs = new Map<string, RunningJob>();

export function startCrawler(opts: RunCrawlerOptions): RunningJob {
  if (!fs.existsSync(LOG_DIR)) fs.mkdirSync(LOG_DIR, { recursive: true });
  const jobId = `${opts.siteId}-${opts.docType ?? "all"}-${Date.now()}`;
  const logPath = path.join(LOG_DIR, `ui_${jobId}.log`);
  const args = ["-m", "crawler.main", "crawl", opts.siteId];
  if (opts.docType) args.push("--doc-type", opts.docType);
  if (opts.incremental) args.push("--incremental");
  if (opts.limit) args.push("--limit", String(opts.limit));

  const out = fs.openSync(logPath, "a");
  const child = spawn(PYTHON_BIN, args, {
    cwd: PROJECT_ROOT,
    detached: true,
    stdio: ["ignore", out, out],
  });
  child.unref();

  const job: RunningJob = {
    jobId,
    pid: child.pid!,
    siteId: opts.siteId,
    logPath,
    startedAt: new Date().toISOString(),
  };
  jobs.set(jobId, job);
  return job;
}

export function stopCrawler(jobId: string): boolean {
  const job = jobs.get(jobId);
  if (!job) return false;
  try {
    process.kill(job.pid, "SIGTERM");
  } catch {
    return false;
  }
  jobs.delete(jobId);
  return true;
}

export function listRunningJobs(): RunningJob[] {
  // Verify each PID is still alive
  for (const [id, job] of jobs) {
    try {
      process.kill(job.pid, 0);
    } catch {
      jobs.delete(id);
    }
  }
  return [...jobs.values()];
}

export function readLogTail(logPath: string, maxLines = 200): string {
  if (!fs.existsSync(logPath)) return "";
  const content = fs.readFileSync(logPath, "utf8");
  const lines = content.split("\n");
  return lines.slice(-maxLines).join("\n");
}

/** List existing gap-fill / crawler log files in .cache/ */
export function listCacheLogs(): Array<{ name: string; size: number; mtime: string }> {
  if (!fs.existsSync(LOG_DIR)) return [];
  const out: Array<{ name: string; size: number; mtime: string }> = [];
  for (const name of fs.readdirSync(LOG_DIR)) {
    if (!name.endsWith(".log")) continue;
    const stat = fs.statSync(path.join(LOG_DIR, name));
    out.push({ name, size: stat.size, mtime: stat.mtime.toISOString() });
  }
  out.sort((a, b) => (a.mtime < b.mtime ? 1 : -1));
  return out;
}
