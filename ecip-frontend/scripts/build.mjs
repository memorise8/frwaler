import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { renderDocument } from "../src/render.mjs";

const outputDirectory = resolve("dist");
const outputFile = resolve(outputDirectory, "index.html");

await mkdir(outputDirectory, { recursive: true });
await writeFile(outputFile, `${renderDocument()}\n`, "utf8");
console.log(`Built ${outputFile}`);
