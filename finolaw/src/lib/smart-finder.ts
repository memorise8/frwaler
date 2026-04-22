import { spawn } from "node:child_process";
import path from "node:path";

const PROJECT_ROOT = path.join(process.cwd(), "..");
const PYTHON_BIN = path.join(PROJECT_ROOT, ".venv", "bin", "python");

export type SmartFindEvent =
  | { type: "progress"; message: string }
  | {
      type: "result";
      url: string;
      documents: FoundDocument[];
      pages_scanned: number;
      detail_pages_visited: number;
      fetch_method: string;
      errors: string[];
    }
  | { type: "db_saved"; count: number; site_id: string }
  | { type: "error"; message: string };

export interface FoundDocument {
  id: string;
  title: string;
  file_url: string;
  file_type: string;
  source_page: string;
  date: string | null;
}

export interface SmartFindOptions {
  url: string;
  maxPages?: number;
  maxDepth?: number;
  delay?: number;
  provider?: "gpt" | "gemini";
  noAi?: boolean;
  saveDb?: boolean;
}

/**
 * Spawn `python -m crawler.main smart-find` and stream NDJSON events.
 * Returns an async generator of parsed events.
 */
export async function* streamSmartFind(
  opts: SmartFindOptions,
  signal?: AbortSignal
): AsyncGenerator<SmartFindEvent> {
  const args = [
    "-m",
    "crawler.main",
    "smart-find",
    opts.url,
    "--max-pages",
    String(opts.maxPages ?? 20),
    "--max-depth",
    String(opts.maxDepth ?? 3),
    "--delay",
    String(opts.delay ?? 1.0),
  ];
  if (opts.provider) args.push("--provider", opts.provider);
  if (opts.noAi) args.push("--no-ai");
  if (opts.saveDb) args.push("--save-db");

  const child = spawn(PYTHON_BIN, args, {
    cwd: PROJECT_ROOT,
    env: { ...process.env },
  });

  signal?.addEventListener("abort", () => child.kill("SIGTERM"));

  let buffer = "";
  const queue: SmartFindEvent[] = [];
  let resolveNext: ((v: void) => void) | null = null;
  let done = false;
  let error: Error | null = null;

  const notify = () => {
    if (resolveNext) {
      const r = resolveNext;
      resolveNext = null;
      r();
    }
  };

  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk: string) => {
    buffer += chunk;
    let nl: number;
    while ((nl = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, nl).trim();
      buffer = buffer.slice(nl + 1);
      if (!line) continue;
      try {
        queue.push(JSON.parse(line) as SmartFindEvent);
        notify();
      } catch {
        // ignore non-JSON lines
      }
    }
  });

  child.stderr.on("data", (chunk: Buffer) => {
    // stderr is progress log from smart_finder; ignore for now
    void chunk;
  });

  child.on("error", (err) => {
    error = err;
    done = true;
    notify();
  });

  child.on("close", () => {
    done = true;
    notify();
  });

  while (true) {
    while (queue.length > 0) {
      yield queue.shift()!;
    }
    if (done) {
      if (error) throw error;
      return;
    }
    await new Promise<void>((r) => {
      resolveNext = r;
    });
  }
}
