import { importProductCsv, getProductsSummary } from "@/lib/products";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type ImportLike = ReturnType<typeof importProductCsv>;

const MOUSER_HOST_RE = /(^|\.)mouser\.(kr|com)$/i;

function emptyFileResult(fileName: string, error: string): ImportLike {
  return {
    fileName,
    rowsRead: 0,
    inserted: 0,
    updated: 0,
    skipped: 0,
    errors: [error],
  };
}

function totalsFor(results: ImportLike[]) {
  return results.reduce(
    (acc, item) => ({
      rowsRead: acc.rowsRead + item.rowsRead,
      inserted: acc.inserted + item.inserted,
      updated: acc.updated + item.updated,
      skipped: acc.skipped + item.skipped,
      errors: acc.errors + item.errors.length,
    }),
    { rowsRead: 0, inserted: 0, updated: 0, skipped: 0, errors: 0 }
  );
}

function parseFileName(value: string | null, fallback: string): string {
  if (!value) return fallback;
  const utf8 = value.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8?.[1]) return decodeURIComponent(utf8[1]).replace(/[\\/]/g, "-");
  const quoted = value.match(/filename="?([^";]+)"?/i);
  if (quoted?.[1]) return quoted[1].replace(/[\\/]/g, "-");
  return fallback;
}

async function handleDownloadJson(req: Request) {
  const body = (await req.json()) as {
    source?: unknown;
    sourceQuery?: unknown;
    urls?: unknown;
    cookie?: unknown;
  };
  const source = typeof body.source === "string" && body.source.trim()
    ? body.source.trim()
    : "mouser";
  const sourceQuery = typeof body.sourceQuery === "string" ? body.sourceQuery.trim() : "";
  const cookie = typeof body.cookie === "string" ? body.cookie.trim() : "";
  const urls = Array.isArray(body.urls)
    ? body.urls.filter((value): value is string => typeof value === "string")
    : [];

  if (urls.length === 0) {
    return Response.json({ error: "download urls required" }, { status: 400 });
  }
  if (urls.length > 20) {
    return Response.json({ error: "한 번에 최대 20개 URL까지 처리합니다." }, { status: 400 });
  }
  if (!cookie) {
    return Response.json({ error: "browser Cookie header required" }, { status: 400 });
  }

  const results: ImportLike[] = [];
  for (const [index, rawUrl] of urls.entries()) {
    let url: URL;
    try {
      url = new URL(rawUrl);
    } catch {
      results.push(emptyFileResult(`url-${index + 1}.csv`, "invalid URL"));
      continue;
    }
    if (url.protocol !== "https:" || !MOUSER_HOST_RE.test(url.hostname)) {
      results.push(emptyFileResult(`url-${index + 1}.csv`, "Mouser HTTPS URL만 허용합니다."));
      continue;
    }
    if (!url.searchParams.has("download")) {
      results.push(emptyFileResult(`url-${index + 1}.csv`, "download 파라미터가 없습니다."));
      continue;
    }

    try {
      const res = await fetch(url, {
        redirect: "follow",
        headers: {
          "user-agent":
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
          accept: "text/csv,application/csv,text/plain,*/*",
          "accept-language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
          referer: `${url.origin}${url.pathname}`,
          cookie,
        },
      });
      const text = await res.text();
      const contentType = res.headers.get("content-type") ?? "";
      const fileName = parseFileName(
        res.headers.get("content-disposition"),
        `mouser-download-${index + 1}.csv`
      );
      if (!res.ok) {
        results.push(emptyFileResult(fileName, `HTTP ${res.status}`));
        continue;
      }
      if (/text\/html/i.test(contentType) && /access to this page has been denied|captcha|interstitial/i.test(text)) {
        results.push(emptyFileResult(fileName, "Mouser가 이 세션을 차단했습니다."));
        continue;
      }
      results.push(
        importProductCsv({
          fileName,
          text,
          source,
          sourceQuery,
        })
      );
    } catch (error) {
      results.push(
        emptyFileResult(
          `mouser-download-${index + 1}.csv`,
          error instanceof Error ? error.message : String(error)
        )
      );
    }
  }

  return Response.json({
    totals: totalsFor(results),
    files: results,
    summary: getProductsSummary(),
  });
}

export async function GET() {
  return Response.json(getProductsSummary());
}

export async function POST(req: Request) {
  const contentType = req.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    return handleDownloadJson(req);
  }

  const form = await req.formData();
  const source = String(form.get("source") ?? "mouser").trim() || "mouser";
  const sourceQuery = String(form.get("sourceQuery") ?? "").trim();
  const files = form
    .getAll("files")
    .filter((value): value is File => value instanceof File);

  if (files.length === 0) {
    return Response.json({ error: "CSV files required" }, { status: 400 });
  }

  const results = [];
  for (const file of files) {
    if (!file.name.toLowerCase().endsWith(".csv")) {
      results.push({
        fileName: file.name,
        rowsRead: 0,
        inserted: 0,
        updated: 0,
        skipped: 0,
        errors: ["CSV 파일만 업로드할 수 있습니다."],
      });
      continue;
    }

    const text = Buffer.from(await file.arrayBuffer()).toString("utf8");
    results.push(
      importProductCsv({
        fileName: file.name,
        text,
        source,
        sourceQuery,
      })
    );
  }

  return Response.json({
    totals: totalsFor(results),
    files: results,
    summary: getProductsSummary(),
  });
}
