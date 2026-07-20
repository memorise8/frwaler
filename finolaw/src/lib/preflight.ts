import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

export type PreflightLevel = "ok" | "warning" | "error";

export interface PreflightCheck {
  id: string;
  label: string;
  level: PreflightLevel;
  message: string;
  detail?: string;
}

export interface PreflightStatus {
  status: PreflightLevel;
  projectRoot: string;
  checkedAt: string;
  checks: PreflightCheck[];
}

const PROJECT_ROOT = path.resolve(process.cwd(), "..");
const DB_PATH = path.join(PROJECT_ROOT, "data", "libertree.db");
const PYTHON_BIN = path.join(PROJECT_ROOT, ".venv", "bin", "python");
const CRAWLER_DIR = path.join(PROJECT_ROOT, "crawler");
const CACHE_DIR = path.join(PROJECT_ROOT, ".cache");
const CRAWLER_ENV = path.join(CRAWLER_DIR, ".env");

function readEnvFileValue(filePath: string, key: string): string | null {
  try {
    const raw = fs.readFileSync(filePath, "utf8");
    for (const line of raw.split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#")) continue;
      const eq = trimmed.indexOf("=");
      if (eq <= 0) continue;
      const k = trimmed.slice(0, eq).trim();
      if (k !== key) continue;
      return trimmed.slice(eq + 1).trim().replace(/^['"]|['"]$/g, "");
    }
  } catch {
    // ignore missing/unreadable env file
  }
  return null;
}

function commandAvailable(command: string, args: string[] = ["--version"]): boolean {
  const result = spawnSync(command, args, {
    stdio: "ignore",
    env: process.env,
  });
  return result.status === 0;
}

function checkFile(pathname: string, id: string, label: string, missingLevel: PreflightLevel): PreflightCheck {
  if (fs.existsSync(pathname)) {
    return {
      id,
      label,
      level: "ok",
      message: "확인됨",
      detail: pathname,
    };
  }
  return {
    id,
    label,
    level: missingLevel,
    message: "찾을 수 없음",
    detail: pathname,
  };
}

export function getPreflightStatus(): PreflightStatus {
  const openAiKey = process.env.OPENAI_API_KEY || readEnvFileValue(CRAWLER_ENV, "OPENAI_API_KEY");
  const authEnabled = Boolean(process.env.ADMIN_USER && process.env.ADMIN_PASSWORD);
  const codexAvailable = commandAvailable("codex");

  const checks: PreflightCheck[] = [
    checkFile(CRAWLER_DIR, "crawler-dir", "Python crawler package", "error"),
    checkFile(PYTHON_BIN, "python", "Python runtime", "error"),
    checkFile(DB_PATH, "database", "SQLite database", "warning"),
    checkFile(CACHE_DIR, "cache-dir", "Log cache directory", "warning"),
    {
      id: "openai-api-key",
      label: "OpenAI API key",
      level: openAiKey ? "ok" : "warning",
      message: openAiKey ? "설정됨" : "미설정 — Auto-Add/Codex 사용 전 설정 필요",
      detail: openAiKey ? "OPENAI_API_KEY present" : `env or ${CRAWLER_ENV}`,
    },
    {
      id: "codex-cli",
      label: "Codex CLI",
      level: codexAvailable ? "ok" : "warning",
      message: codexAvailable ? "사용 가능" : "PATH에서 찾을 수 없음 — Auto-Add Codex 경로 사용 불가",
      detail: "codex --version",
    },
    {
      id: "admin-auth",
      label: "Admin authentication",
      level: authEnabled ? "ok" : "warning",
      message: authEnabled ? "HTTP Basic Auth 활성화" : "비활성화 — 외부 노출 금지",
      detail: "ADMIN_USER / ADMIN_PASSWORD",
    },
  ];

  const hasError = checks.some((check) => check.level === "error");
  const hasWarning = checks.some((check) => check.level === "warning");

  return {
    status: hasError ? "error" : hasWarning ? "warning" : "ok",
    projectRoot: PROJECT_ROOT,
    checkedAt: new Date().toISOString(),
    checks,
  };
}
