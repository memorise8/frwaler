import { cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";

await rm("dist", { force: true, recursive: true });
await mkdir("dist", { recursive: true });
await Promise.all([
  cp("index.html", "dist/index.html"),
  cp("src/styles.css", "dist/styles.css"),
  cp("src/app.js", "dist/assets/app.js"),
]);
const catalogue = await readFile("data/catalog.json", "utf8");
await mkdir("dist/assets", { recursive: true });
await writeFile("dist/assets/catalogue.js", `export const catalogue = ${catalogue};\n`);
