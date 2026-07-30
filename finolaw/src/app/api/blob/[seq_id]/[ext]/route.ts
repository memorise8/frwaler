import fs from "fs";
import { blobAbsolutePath, getDocumentBlobInfo } from "@/lib/db";
import { authenticateInternalRequest, internalAuthFailure } from "@/lib/internal-auth";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const ALLOWED_EXT = new Set(["pdf", "txt"]);

interface Params {
  seq_id: string;
  ext: string;
}

function parseSeqId(raw: string): number | null {
  if (!/^\d{1,12}$/.test(raw)) return null;
  const n = Number.parseInt(raw, 10);
  if (!Number.isInteger(n) || n < 0 || n >= 1e12) return null;
  return n;
}

function notFound(message: string): Response {
  return new Response(message, {
    status: 404,
    headers: { "content-type": "text/plain; charset=utf-8" },
  });
}

function buildFilename(
  seqId: number,
  ext: "pdf" | "txt",
  original: string | null
): string {
  const pad = seqId.toString().padStart(12, "0");
  if (ext === "pdf" && original && original.trim()) {
    // sanitize a bit — strip CR/LF/quotes from header value
    const safe = original.replace(/[\r\n"\\]/g, "").trim();
    if (safe) return safe;
  }
  return `${pad}.${ext}`;
}

function rfc5987(value: string): string {
  return encodeURIComponent(value).replace(/['()]/g, escape).replace(/\*/g, "%2A");
}

export async function GET(
  req: Request,
  { params }: { params: Promise<Params> }
): Promise<Response> {
  const auth = authenticateInternalRequest(req);
  if (auth.kind !== "authorized") {
    const failure = internalAuthFailure(auth);
    return new Response(failure.body, failure);
  }

  const { seq_id: seqIdRaw, ext: extRaw } = await params;

  const seqId = parseSeqId(seqIdRaw);
  if (seqId === null) return notFound("invalid seq_id");

  const ext = extRaw.toLowerCase();
  if (!ALLOWED_EXT.has(ext)) return notFound("unsupported extension");
  const typedExt = ext as "pdf" | "txt";

  const info = getDocumentBlobInfo(seqId);
  if (!info) return notFound("blob not found");

  const available = typedExt === "pdf" ? info.pdf_downloaded : info.text_extracted;
  if (!available) return notFound("blob not found");

  let absPath: string;
  try {
    absPath = blobAbsolutePath(seqId, typedExt);
  } catch {
    return notFound("invalid seq_id");
  }

  let stat: fs.Stats;
  try {
    stat = fs.statSync(absPath);
  } catch {
    return notFound(`blob not found: ${seqId}.${ext}`);
  }
  if (!stat.isFile()) return notFound("not a file");

  const filename = buildFilename(seqId, typedExt, info.original_filename);

  const contentType =
    typedExt === "pdf" ? "application/pdf" : "text/plain; charset=utf-8";

  // Stream from disk. Node's ReadStream is async-iterable; convert to a Web
  // ReadableStream so the Response constructor accepts it.
  const nodeStream = fs.createReadStream(absPath);
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      nodeStream.on("data", (chunk: Buffer | string) => {
        if (typeof chunk === "string") {
          controller.enqueue(new TextEncoder().encode(chunk));
        } else {
          controller.enqueue(
            new Uint8Array(
              chunk.buffer,
              chunk.byteOffset,
              chunk.byteLength
            )
          );
        }
      });
      nodeStream.on("end", () => controller.close());
      nodeStream.on("error", (err) => controller.error(err));
    },
    cancel() {
      nodeStream.destroy();
    },
  });

  return new Response(body, {
    status: 200,
    headers: {
      "content-type": contentType,
      "content-length": String(stat.size),
      "content-disposition": `inline; filename="${filename.replace(
        /[^\x20-\x7e]/g,
        "_"
      )}"; filename*=UTF-8''${rfc5987(filename)}`,
      "cache-control": "private, max-age=0, must-revalidate",
    },
  });
}
