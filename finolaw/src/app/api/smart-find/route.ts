import { NextRequest } from "next/server";
import { streamSmartFind, type SmartFindEvent } from "@/lib/smart-finder";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST(req: NextRequest) {
  const body = (await req.json()) as {
    url?: string;
    maxPages?: number;
    maxDepth?: number;
    delay?: number;
    provider?: "gpt" | "gemini";
    noAi?: boolean;
    saveDb?: boolean;
  };

  if (!body.url || !/^https?:\/\//.test(body.url)) {
    return new Response(
      JSON.stringify({ error: "valid http(s) url required" }),
      { status: 400, headers: { "content-type": "application/json" } }
    );
  }

  const encoder = new TextEncoder();
  const stream = new ReadableStream({
    async start(controller) {
      const send = (event: SmartFindEvent | { type: string; [k: string]: unknown }) => {
        controller.enqueue(
          encoder.encode(`data: ${JSON.stringify(event)}\n\n`)
        );
      };

      try {
        const abortController = new AbortController();
        req.signal.addEventListener("abort", () => abortController.abort());

        for await (const ev of streamSmartFind(
          {
            url: body.url!,
            maxPages: body.maxPages,
            maxDepth: body.maxDepth,
            delay: body.delay,
            provider: body.provider,
            noAi: body.noAi,
            saveDb: body.saveDb,
          },
          abortController.signal
        )) {
          send(ev);
        }
        send({ type: "done" });
      } catch (err) {
        send({
          type: "error",
          message: err instanceof Error ? err.message : String(err),
        });
      } finally {
        controller.close();
      }
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
