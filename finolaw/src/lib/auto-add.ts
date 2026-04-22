import { spawn } from "node:child_process";
import path from "node:path";
import fs from "node:fs";

const PROJECT_ROOT = path.join(process.cwd(), "..");
const PYTHON_BIN = path.join(PROJECT_ROOT, ".venv", "bin", "python");
const LOG_DIR = path.join(PROJECT_ROOT, ".cache");

export interface AutoAddJob {
  jobId: string;
  pid: number;
  url: string;
  siteId: string | null;
  browser: boolean;
  dryRun: boolean;
  logPath: string;
  startedAt: string;
}

const jobs = new Map<string, AutoAddJob>();

export function startAutoAdd(opts: {
  url: string;
  siteId?: string;
  browser?: boolean;
  dryRun?: boolean;
}): AutoAddJob {
  if (!fs.existsSync(LOG_DIR)) fs.mkdirSync(LOG_DIR, { recursive: true });

  const jobId = `autoadd-${Date.now()}`;
  const logPath = path.join(LOG_DIR, `ui_${jobId}.log`);

  const args = ["-m", "crawler.main", "auto-add", opts.url];
  if (opts.siteId) args.push("--site-id", opts.siteId);
  if (opts.browser) args.push("--browser");
  if (opts.dryRun) args.push("--dry-run");

  const out = fs.openSync(logPath, "a");
  const child = spawn(PYTHON_BIN, args, {
    cwd: PROJECT_ROOT,
    detached: true,
    stdio: ["ignore", out, out],
  });
  child.unref();

  const job: AutoAddJob = {
    jobId,
    pid: child.pid!,
    url: opts.url,
    siteId: opts.siteId ?? null,
    browser: opts.browser ?? false,
    dryRun: opts.dryRun ?? false,
    logPath,
    startedAt: new Date().toISOString(),
  };
  jobs.set(jobId, job);
  return job;
}

export function stopAutoAdd(jobId: string): boolean {
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

export function listAutoAddJobs(): AutoAddJob[] {
  for (const [id, job] of jobs) {
    try {
      process.kill(job.pid, 0);
    } catch {
      jobs.delete(id);
    }
  }
  return [...jobs.values()];
}
