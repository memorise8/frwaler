import { NextRequest } from "next/server";
import {
  startAutoAdd,
  stopAutoAdd,
  listAutoAddJobs,
} from "@/lib/auto-add";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  return Response.json({ jobs: listAutoAddJobs() });
}

export async function POST(req: NextRequest) {
  const body = (await req.json()) as {
    action?: string;
    url?: string;
    siteId?: string;
    browser?: boolean;
    dryRun?: boolean;
    jobId?: string;
  };

  if (body.action === "start") {
    if (!body.url || !/^https?:\/\//.test(body.url)) {
      return Response.json(
        { error: "valid http(s) url required" },
        { status: 400 }
      );
    }
    const job = startAutoAdd({
      url: body.url,
      siteId: body.siteId,
      browser: body.browser,
      dryRun: body.dryRun,
    });
    return Response.json({ job });
  }

  if (body.action === "stop") {
    if (!body.jobId) {
      return Response.json({ error: "jobId required" }, { status: 400 });
    }
    const stopped = stopAutoAdd(body.jobId);
    return Response.json({ stopped });
  }

  return Response.json({ error: "unknown action" }, { status: 400 });
}
