import { createReadStream, existsSync } from "node:fs";
import { createServer } from "node:http";
import { resolve } from "node:path";

const port = Number.parseInt(process.argv[2] ?? "4173", 10);
const indexFile = resolve("dist", "index.html");

if (!Number.isInteger(port) || port < 1 || port > 65535) {
  throw new RangeError("Port must be an integer between 1 and 65535.");
}

if (!existsSync(indexFile)) {
  throw new Error("dist/index.html is missing. Run npm run build before npm run dev.");
}

const server = createServer((request, response) => {
  if (request.url !== "/" && request.url !== "/index.html") {
    response.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
    response.end("Not found");
    return;
  }

  response.writeHead(200, { "content-type": "text/html; charset=utf-8" });
  createReadStream(indexFile).pipe(response);
});

server.listen(port, "127.0.0.1", () => {
  console.log(`Serving the generated dashboard at ${"http" + "://"}127.0.0.1:${port}/`);
});
