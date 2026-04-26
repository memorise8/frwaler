import { getPreflightStatus } from "@/lib/preflight";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  const health = getPreflightStatus();
  const status = health.status === "error" ? 503 : 200;
  return Response.json(health, { status });
}
