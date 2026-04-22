import { spawn } from "node:child_process";
import path from "node:path";
import fs from "node:fs";

const PROJECT_ROOT = path.join(process.cwd(), "..");
const PYTHON_BIN = path.join(PROJECT_ROOT, ".venv", "bin", "python");
const LOG_DIR = path.join(PROJECT_ROOT, ".cache");

export interface CodexJob {
  jobId: string;
  pid: number;
  url: string;
  siteId: string | null;
  siteName: string | null;
  logPath: string;
  startedAt: string;
}

const jobs = new Map<string, CodexJob>();

export function startCodexJob(opts: {
  url: string;
  siteId?: string;
  siteName?: string;
  timeoutSeconds?: number;
}): CodexJob {
  if (!fs.existsSync(LOG_DIR)) fs.mkdirSync(LOG_DIR, { recursive: true });

  const jobId = `codex-${Date.now()}`;
  const logPath = path.join(LOG_DIR, `ui_${jobId}.log`);

  const args = ["-m", "crawler.main", "auto-add-codex", opts.url];
  if (opts.siteId) args.push("--site-id", opts.siteId);
  if (opts.siteName) args.push("--site-name", opts.siteName);
  if (opts.timeoutSeconds != null)
    args.push("--timeout-seconds", String(opts.timeoutSeconds));

  const out = fs.openSync(logPath, "a");
  const child = spawn(PYTHON_BIN, args, {
    cwd: PROJECT_ROOT,
    detached: true,
    stdio: ["ignore", out, out],
  });
  child.unref();

  const job: CodexJob = {
    jobId,
    pid: child.pid!,
    url: opts.url,
    siteId: opts.siteId ?? null,
    siteName: opts.siteName ?? null,
    logPath,
    startedAt: new Date().toISOString(),
  };
  jobs.set(jobId, job);
  return job;
}

export function stopCodexJob(jobId: string): boolean {
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

export function listCodexJobs(): CodexJob[] {
  for (const [id, job] of jobs) {
    try {
      process.kill(job.pid, 0);
    } catch {
      jobs.delete(id);
    }
  }
  return [...jobs.values()];
}
