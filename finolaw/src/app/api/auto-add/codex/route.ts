import { NextRequest } from "next/server";
import {
  startCodexJob,
  stopCodexJob,
  listCodexJobs,
} from "@/lib/auto-add-codex";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  return Response.json({ jobs: listCodexJobs() });
}

export async function POST(req: NextRequest) {
  const body = (await req.json()) as {
    action?: string;
    url?: string;
    siteId?: string;
    siteName?: string;
    timeoutSeconds?: number;
    jobId?: string;
  };

  if (body.action === "start") {
    if (!body.url || !/^https?:\/\//.test(body.url)) {
      return Response.json(
        { error: "valid http(s) url required" },
        { status: 400 }
      );
    }
    const job = startCodexJob({
      url: body.url,
      siteId: body.siteId,
      siteName: body.siteName,
      timeoutSeconds: body.timeoutSeconds,
    });
    return Response.json({ job });
  }

  if (body.action === "stop") {
    if (!body.jobId) {
      return Response.json({ error: "jobId required" }, { status: 400 });
    }
    const stopped = stopCodexJob(body.jobId);
    return Response.json({ stopped });
  }

  return Response.json({ error: "unknown action" }, { status: 400 });
}
