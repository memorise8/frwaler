import { getRecentPapers, renderPapersMarkdown } from "@/lib/db";

function sanitizeFilePart(value: string): string {
  return value.replace(/[^a-zA-Z0-9_-]+/g, "-").replace(/^-+|-+$/g, "") || "all-sites";
}

export async function GET(request: Request) {
  const url = new URL(request.url);
  const site = url.searchParams.get("site")?.trim() || undefined;
  const limitParam = url.searchParams.get("limit");
  const parsedLimit = limitParam ? Number.parseInt(limitParam, 10) : 200;
  const limit = Number.isFinite(parsedLimit) ? parsedLimit : 200;

  const papers = getRecentPapers(site, limit);
  const title = site ? `${site} documents` : "all collected documents";
  const markdown = renderPapersMarkdown(papers, title);
  const fileName = `${sanitizeFilePart(site ?? "all-sites")}.md`;

  return new Response(markdown, {
    headers: {
      "content-type": "text/markdown; charset=utf-8",
      "content-disposition": `attachment; filename="${fileName}"`,
      "cache-control": "no-store",
    },
  });
}
