import { spawn } from "node:child_process";
import path from "node:path";
import { getDocumentById } from "@/lib/db";

// Resolve the repo root (one level above finolaw/) so we run the crawler
// against the actual data/papers.db that the dashboard reads.
const REPO_ROOT = path.resolve(process.cwd(), "..");
const PYTHON = process.env.PYTHON_BIN || "python3";

interface SummarizeBody {
  docId?: number | string;
  provider?: "gpt" | "openai" | "gemini" | "google" | null;
}

function parseDocId(raw: unknown): number | null {
  if (typeof raw === "number" && Number.isInteger(raw) && raw > 0) return raw;
  if (typeof raw === "string" && /^\d+$/.test(raw)) {
    const n = Number(raw);
    if (Number.isInteger(n) && n > 0) return n;
  }
  return null;
}

function runSummarize(
  docId: number,
  provider: string | null | undefined,
  timeoutMs: number,
): Promise<{ code: number; stdout: string; stderr: string }> {
  const args = [
    "-m", "crawler.main", "summarize",
    "--doc-id", String(docId),
  ];
  if (provider) {
    args.push("--provider", provider);
  }

  return new Promise((resolve, reject) => {
    const child = spawn(PYTHON, args, {
      cwd: REPO_ROOT,
      env: process.env,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      child.kill("SIGTERM");
      reject(new Error(`summarize process timed out after ${timeoutMs}ms`));
    }, timeoutMs);
    child.stdout.on("data", (b: Buffer) => { stdout += b.toString("utf8"); });
    child.stderr.on("data", (b: Buffer) => { stderr += b.toString("utf8"); });
    child.on("error", (err) => {
      clearTimeout(timer);
      reject(err);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code: code ?? -1, stdout, stderr });
    });
  });
}

export async function POST(request: Request) {
  let body: SummarizeBody;
  try {
    body = (await request.json()) as SummarizeBody;
  } catch {
    return Response.json(
      { ok: false, error: "request body must be JSON" },
      { status: 400 },
    );
  }

  const docId = parseDocId(body?.docId);
  if (docId === null) {
    return Response.json(
      { ok: false, error: "docId must be a positive integer" },
      { status: 400 },
    );
  }

  // Confirm row exists before spawning the python process — this also
  // gives us the pre-summary state to diff against in the response.
  const before = getDocumentById(docId);
  if (!before) {
    return Response.json(
      { ok: false, error: `no document with id=${docId}` },
      { status: 404 },
    );
  }

  let result;
  try {
    result = await runSummarize(docId, body.provider ?? null, 90_000);
  } catch (err) {
    return Response.json(
      {
        ok: false,
        error: err instanceof Error ? err.message : String(err),
      },
      { status: 500 },
    );
  }

  if (result.code !== 0) {
    return Response.json(
      {
        ok: false,
        error: `summarize exited with code ${result.code}`,
        stderr: result.stderr.slice(-2000),
      },
      { status: 500 },
    );
  }

  const after = getDocumentById(docId);
  return Response.json({
    ok: true,
    docId,
    summary: after?.summary ?? null,
    changed: (before.summary || "") !== (after?.summary || ""),
    log: result.stdout.slice(-2000),
  });
}
