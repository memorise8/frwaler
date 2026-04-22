import { NextRequest } from "next/server";
import fs from "node:fs";
import path from "node:path";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const LOG_DIR = path.join(process.cwd(), "..", ".cache");

/**
 * SSE stream of log file tail. Emits one `data: <line>` per new line.
 * Query: ?file=<name> (sanitized against LOG_DIR)
 */
export async function GET(req: NextRequest) {
  const name = new URL(req.url).searchParams.get("file");
  if (!name || /[\\/]/.test(name) || !name.endsWith(".log")) {
    return new Response("invalid file", { status: 400 });
  }
  const logPath = path.resolve(LOG_DIR, name);
  if (!logPath.startsWith(path.resolve(LOG_DIR))) {
    return new Response("forbidden", { status: 403 });
  }
  if (!fs.existsSync(logPath)) {
    return new Response("not found", { status: 404 });
  }

  const encoder = new TextEncoder();
  let fd: number | null = null;
  let watcher: fs.FSWatcher | null = null;

  const stream = new ReadableStream({
    async start(controller) {
      const send = (line: string) =>
        controller.enqueue(encoder.encode(`data: ${line}\n\n`));

      try {
        // Emit last ~100 lines initially
        const initial = fs.readFileSync(logPath, "utf8");
        const lastLines = initial.split("\n").slice(-100);
        for (const l of lastLines) if (l) send(l);

        fd = fs.openSync(logPath, "r");
        let position = fs.fstatSync(fd).size;
        watcher = fs.watch(logPath, { persistent: false }, () => {
          try {
            const stat = fs.fstatSync(fd!);
            if (stat.size < position) {
              // truncated
              position = 0;
            }
            const diff = stat.size - position;
            if (diff <= 0) return;
            const buf = Buffer.alloc(diff);
            fs.readSync(fd!, buf, 0, diff, position);
            position = stat.size;
            const text = buf.toString("utf8");
            const lines = text.split("\n");
            for (const l of lines) if (l) send(l);
          } catch {
            // ignore read errors
          }
        });

        req.signal.addEventListener("abort", () => {
          try {
            watcher?.close();
          } catch {}
          try {
            if (fd !== null) fs.closeSync(fd);
          } catch {}
          try {
            controller.close();
          } catch {}
        });
      } catch (err) {
        send(
          `[stream error] ${err instanceof Error ? err.message : String(err)}`
        );
        controller.close();
      }
    },
    cancel() {
      try {
        watcher?.close();
      } catch {}
      try {
        if (fd !== null) fs.closeSync(fd);
      } catch {}
    },
  });

  return new Response(stream, {
    headers: {
      "content-type": "text/event-stream",
      "cache-control": "no-cache, no-transform",
      "x-accel-buffering": "no",
      connection: "keep-alive",
    },
  });
}
