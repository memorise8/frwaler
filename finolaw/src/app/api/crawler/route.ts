import { NextRequest } from "next/server";
import {
  startCrawler,
  stopCrawler,
  listRunningJobs,
  listCacheLogs,
} from "@/lib/crawler-runner";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// Accept any plausible site_id: lowercase alphanumeric, hyphens, underscores, 2-64 chars
const SITE_ID_RE = /^[a-z0-9][a-z0-9\-_]{1,63}$/;

export async function GET() {
  return Response.json({
    jobs: listRunningJobs(),
    logs: listCacheLogs(),
  });
}

export async function POST(req: NextRequest) {
  const body = (await req.json()) as {
    action?: string;
    siteId?: string;
    docType?: string;
    incremental?: boolean;
    limit?: number;
    jobId?: string;
  };

  if (body.action === "start") {
    if (!body.siteId || !SITE_ID_RE.test(body.siteId)) {
      return Response.json({ error: "invalid siteId" }, { status: 400 });
    }
    const job = startCrawler({
      siteId: body.siteId,
      docType: body.docType,
      incremental: body.incremental,
      limit: body.limit,
    });
    return Response.json({ job });
  }

  if (body.action === "stop") {
    if (!body.jobId) {
      return Response.json({ error: "jobId required" }, { status: 400 });
    }
    const ok = stopCrawler(body.jobId);
    return Response.json({ stopped: ok });
  }

  return Response.json({ error: "unknown action" }, { status: 400 });
}
